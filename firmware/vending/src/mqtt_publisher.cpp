#include "mqtt_publisher.h"

#include <stdio.h>
#include <string.h>

#include <atomic>

#include "channels.h"
#include "esp_log.h"
#include "mqtt_client.h"
#include "wire.h"

namespace {
const char* TAG = "mqtt";
constexpr size_t kMaxInbound = 4096;

esp_mqtt_client_handle_t g_client = nullptr;
MqttMessageCb g_on_message = nullptr;
std::atomic<bool> g_connected{false};
std::atomic<bool> g_edge{false};
char g_lwt_topic[96];
char g_lwt_msg[64];
char g_uri[96];
char g_client_id[48];
char g_rx[kMaxInbound];  // reassembly buffer; the client's task is its only user

void handle_data(esp_mqtt_event_handle_t ev) {
    if (ev->total_data_len > static_cast<int>(sizeof(g_rx))) {
        if (ev->current_data_offset == 0) ESP_LOGW(TAG, "inbound %d bytes too large; dropped", ev->total_data_len);
        return;
    }
    if (ev->current_data_offset + ev->data_len > static_cast<int>(sizeof(g_rx))) return;
    memcpy(g_rx + ev->current_data_offset, ev->data, ev->data_len);
    if (ev->current_data_offset + ev->data_len < ev->total_data_len) return;  // more fragments to come
    // Only the first fragment carries the topic, so a fragmented message is dropped here.
    if (ev->topic_len > 0 && ev->current_data_offset == 0 && g_on_message != nullptr) {
        g_on_message(ev->topic, ev->topic_len, g_rx, ev->total_data_len);
    } else if (ev->topic_len == 0) {
        ESP_LOGW(TAG, "fragmented inbound message dropped (rx buffer smaller than message)");
    }
}

void on_event(void*, esp_event_base_t, int32_t id, void* data) {
    auto* ev = static_cast<esp_mqtt_event_handle_t>(data);
    switch (id) {
        case MQTT_EVENT_CONNECTED:
            g_connected.store(true);
            g_edge.store(true);
            ESP_LOGI(TAG, "connected");
            break;
        case MQTT_EVENT_DISCONNECTED:
            g_connected.store(false);
            ESP_LOGW(TAG, "disconnected");
            break;
        case MQTT_EVENT_DATA: handle_data(ev); break;
        default: break;
    }
}
}  // namespace

void mqtt_start(const Settings& s, MqttMessageCb on_message) {
    g_on_message = on_message;
    if (s.mqtt_host[0] == '\0') {
        ESP_LOGW(TAG, "no mqtt_host configured; MQTT stays down");
        return;
    }
    vendcore::WireCtx ctx;
    ctx.machine_id = s.machine_id;
    char lwt_tail[48];
    snprintf(lwt_tail, sizeof(lwt_tail), "heartbeat/%s", vendcore::kSubsystem);
    vendcore::topic_join(ctx, lwt_tail, g_lwt_topic, sizeof(g_lwt_topic));
    const size_t n = vendcore::lwt_json(g_lwt_msg, sizeof(g_lwt_msg));
    snprintf(g_uri, sizeof(g_uri), "mqtt://%s:%u", s.mqtt_host, static_cast<unsigned>(s.mqtt_port));
    snprintf(g_client_id, sizeof(g_client_id), "vending-%s", s.machine_id);

    esp_mqtt_client_config_t cfg = {};
    cfg.broker.address.uri = g_uri;
    cfg.credentials.client_id = g_client_id;
    if (s.mqtt_user[0] != '\0') {
        cfg.credentials.username = s.mqtt_user;
        cfg.credentials.authentication.password = s.mqtt_pass;
    }
    cfg.session.last_will.topic = g_lwt_topic;
    cfg.session.last_will.msg = g_lwt_msg;
    cfg.session.last_will.msg_len = static_cast<int>(n);
    cfg.session.last_will.qos = 1;
    cfg.session.last_will.retain = false;
    cfg.session.keepalive = 15;
    cfg.buffer.size = 4096;  // capabilities is ~3.6 KB
    cfg.buffer.out_size = 4096;
    g_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(g_client, MQTT_EVENT_ANY, on_event, nullptr);
    esp_mqtt_client_start(g_client);
}

bool mqtt_connected() { return g_connected.load(); }

bool mqtt_take_connected_edge() {
    return g_edge.exchange(false);
}

bool mqtt_publish(const char* topic, const char* payload, size_t len, bool retain) {
    if (!g_connected.load() || g_client == nullptr) return false;
    return esp_mqtt_client_enqueue(g_client, topic, payload, static_cast<int>(len), 1, retain, true) >= 0;
}

bool mqtt_subscribe(const char* topic) {
    return g_client != nullptr && esp_mqtt_client_subscribe(g_client, topic, 1) >= 0;
}
