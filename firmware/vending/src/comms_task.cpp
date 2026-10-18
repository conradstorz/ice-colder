#include "comms_task.h"

#include <stdio.h>
#include <string.h>

#include "ack_cache.h"
#include "channels.h"
#include "command.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mqtt_publisher.h"
#include "net.h"
#include "outbound_ring.h"
#include "queues.h"
#include "version.h"
#include "wire.h"

using namespace vendcore;

namespace {
const char* TAG = "comms";
constexpr uint32_t kHeartbeatMs = 10000;

struct RxItem {
    char topic[96];
    char data[2048];
    uint16_t len;
};

struct Comms {
    QueueHandle_t cmd_q, evt_q, ack_q, rx_q;
    Settings settings;
    AckCache cache;
    OutboundRing ring;
    uint32_t dropped_no_request_id = 0;
    char t_cmd[96], t_online[96], t_ack[96];
};
Comms g;

uint32_t uptime_s() { return static_cast<uint32_t>(esp_timer_get_time() / 1000000); }

WireCtx make_ctx(char* iso, size_t iso_cap) {
    WireCtx c;
    c.machine_id = g.settings.machine_id;
    c.hardware_id = net_hardware_id();
    c.ip = net_ip();
    c.firmware = kFirmwareVersion;
    c.iso_time = net_iso_time(iso, iso_cap) ? iso : nullptr;
    return c;
}

// Publishes now when connected and nothing is queued ahead; otherwise holds it in the ring.
void send(const char* topic, const char* payload, size_t len, bool retain, Prio prio) {
    if (g.ring.size() == 0 && mqtt_publish(topic, payload, len, retain)) return;
    g.ring.push(topic, payload, len, retain, prio);
}

void flush_ring() {
    while (const RingEntry* e = g.ring.front()) {
        if (!mqtt_publish(e->topic, e->payload, e->len, e->retain)) return;
        g.ring.pop();
    }
}

void on_message(const char* topic, size_t tl, const char* data, size_t dl) {  // MQTT client task
    static RxItem item;
    if (tl >= sizeof(item.topic) || dl >= sizeof(item.data)) {
        ESP_LOGW(TAG, "inbound message too large; dropped");
        return;
    }
    memcpy(item.topic, topic, tl);
    item.topic[tl] = '\0';
    memcpy(item.data, data, dl);
    item.data[dl] = '\0';
    item.len = static_cast<uint16_t>(dl);
    xQueueSend(g.rx_q, &item, 0);
}

void publish_ack(const Ack& a) {
    static char buf[768];
    const size_t n = ack_json(a, buf, sizeof(buf));
    if (n == 0) {
        ESP_LOGW(TAG, "ack for %s did not serialize", a.request_id);
        return;
    }
    send(g.t_ack, buf, n, false, Prio::Normal);
}

void push_cmd(const CmdItem& item) {
    if (xQueueSend(g.cmd_q, &item, 0) != pdTRUE) ESP_LOGW(TAG, "cmd queue full, dropped item kind %d", item.kind);
}

void handle_command(const RxItem& rx) {
    static CmdItem item;  // Decoded is large; keep it off the stack
    item.decoded = decode_command(rx.data, rx.len);  // only caller: not reentrant
    if (item.decoded.result == Decode::NoRequestId) {
        ++g.dropped_no_request_id;
        ESP_LOGW(TAG, "command without a usable request_id dropped (%u so far)", static_cast<unsigned>(g.dropped_no_request_id));
        return;
    }
    if (const Ack* hit = g.cache.find(item.decoded.cmd.request_id)) {
        publish_ack(*hit);  // retry of an acked command: replay, never re-run
        return;
    }
    item.kind = CmdItem::Decoded;
    push_cmd(item);
    item.kind = CmdItem::VmcSeen;
    push_cmd(item);
}

void handle_online(const RxItem& rx) {
    const char* k = strstr(rx.data, "\"online\"");
    if (k == nullptr) return;
    k = strchr(k, ':');
    if (k == nullptr) return;
    while (*++k == ' ') {}
    static CmdItem item;
    item.kind = CmdItem::VmcOnline;
    item.online = strncmp(k, "true", 4) == 0;
    push_cmd(item);
}

void on_connect() {
    static char payload[4096];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    size_t n = capabilities_json(ctx, payload, sizeof(payload));
    char cap_tail[48];
    snprintf(cap_tail, sizeof(cap_tail), "capabilities/%s", kSubsystem);
    if (n > 0 && topic_join(ctx, cap_tail, topic, sizeof(topic)) > 0) mqtt_publish(topic, payload, n, true);
    char hb_tail[48];
    snprintf(hb_tail, sizeof(hb_tail), "heartbeat/%s", kSubsystem);
    n = heartbeat_json(uptime_s(), payload, sizeof(payload));
    if (n > 0 && topic_join(ctx, hb_tail, topic, sizeof(topic)) > 0) mqtt_publish(topic, payload, n, false);
    for (int i = 0; i < ha_discovery_count(); ++i) {
        const size_t pn = ha_discovery(i, ctx, topic, sizeof(topic), payload, sizeof(payload));
        if (pn > 0) mqtt_publish(topic, payload, pn, true);
    }
    mqtt_subscribe(g.t_cmd);
    mqtt_subscribe(g.t_online);
    CmdItem fr = {};
    fr.kind = CmdItem::ForceReport;
    push_cmd(fr);
}

void send_event(const Event& e) {
    static char topic[160];
    static char payload[512];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    if (topic_for(e, ctx, topic, sizeof(topic)) == 0) {
        if (e.type == EventType::Health) ESP_LOGW(TAG, "board health: %s", e.text);
        return;
    }
    const size_t n = payload_for(e, ctx, uptime_s(), payload, sizeof(payload));
    if (n == 0) return;
    Prio p = Prio::Normal;
    if (e.type == EventType::Outcome) p = Prio::Outcome;
    else if (e.type == EventType::Telemetry || e.type == EventType::Temperature) p = Prio::Telemetry;
    send(topic, payload, n, false, p);
}

void comms_main(void*) {
    uint32_t last_hb = 0;
    for (;;) {
        static RxItem rx;
        while (xQueueReceive(g.rx_q, &rx, 0) == pdTRUE) {
            if (strcmp(rx.topic, g.t_cmd) == 0) handle_command(rx);
            else if (strcmp(rx.topic, g.t_online) == 0) handle_online(rx);
        }
        if (mqtt_take_connected_edge()) {
            on_connect();
            last_hb = uptime_s();
        }
        static Ack ack;
        while (xQueueReceive(g.ack_q, &ack, 0) == pdTRUE) {
            g.cache.put(ack);
            publish_ack(ack);
        }
        static Event ev;
        while (xQueueReceive(g.evt_q, &ev, 0) == pdTRUE) send_event(ev);
        if (mqtt_connected()) {
            flush_ring();
            if (uptime_s() - last_hb >= kHeartbeatMs / 1000) {
                last_hb = uptime_s();
                static char buf[96];
                static char topic[160];
                char iso[24];
                const WireCtx ctx = make_ctx(iso, sizeof(iso));
                char tail[48];
                snprintf(tail, sizeof(tail), "heartbeat/%s", kSubsystem);
                const size_t n = heartbeat_json(uptime_s(), buf, sizeof(buf));
                if (n > 0 && topic_join(ctx, tail, topic, sizeof(topic)) > 0) send(topic, buf, n, false, Prio::Normal);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
}  // namespace

void start_comms(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q, const Settings& settings) {
    g.cmd_q = cmd_q;
    g.evt_q = evt_q;
    g.ack_q = ack_q;
    g.rx_q = xQueueCreate(3, sizeof(RxItem));
    g.settings = settings;
    WireCtx ctx;
    ctx.machine_id = g.settings.machine_id;
    char tail[48];
    snprintf(tail, sizeof(tail), "cmd/%s", kSubsystem);
    topic_join(ctx, tail, g.t_cmd, sizeof(g.t_cmd));
    snprintf(tail, sizeof(tail), "cmd/%s/ack", kSubsystem);
    topic_join(ctx, tail, g.t_ack, sizeof(g.t_ack));
    topic_join(ctx, "online", g.t_online, sizeof(g.t_online));
    net_start(settings);
    mqtt_start(settings, on_message);
    xTaskCreatePinnedToCore(comms_main, "comms", 8192, nullptr, 5, nullptr, 0);
}
