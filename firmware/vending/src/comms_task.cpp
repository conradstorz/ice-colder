#include "comms_task.h"

#include <stdio.h>
#include <string.h>

#include <atomic>

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
constexpr int kInflightMax = 8;
constexpr uint32_t kRxDropLogMs = 10000;

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
    std::atomic<uint32_t> rx_dropped{0};  // written by the MQTT task, logged by the comms task
    uint32_t rx_dropped_logged = 0;
    // Request ids pushed to cmd_q whose first ack has not yet been drained from ack_q.
    char inflight[kInflightMax][65] = {};
    bool inflight_used[kInflightMax] = {};
    bool caps_pending = false;  // retained capabilities publish not yet accepted
    bool subs_pending = false;
    int ha_next = 0;            // next HA discovery entry to publish
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
        g.rx_dropped.fetch_add(1);
        return;
    }
    memcpy(item.topic, topic, tl);
    item.topic[tl] = '\0';
    memcpy(item.data, data, dl);
    item.data[dl] = '\0';
    item.len = static_cast<uint16_t>(dl);
    if (xQueueSend(g.rx_q, &item, 0) != pdTRUE) g.rx_dropped.fetch_add(1);
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

bool push_cmd(const CmdItem& item) {
    if (xQueueSend(g.cmd_q, &item, 0) == pdTRUE) return true;
    ESP_LOGW(TAG, "cmd queue full, dropped item kind %d", item.kind);
    return false;
}

int inflight_find(const char* id) {
    for (int i = 0; i < kInflightMax; ++i)
        if (g.inflight_used[i] && strcmp(g.inflight[i], id) == 0) return i;
    return -1;
}

bool inflight_add(const char* id) {
    for (int i = 0; i < kInflightMax; ++i) {
        if (g.inflight_used[i]) continue;
        snprintf(g.inflight[i], sizeof(g.inflight[i]), "%s", id);
        g.inflight_used[i] = true;
        return true;
    }
    return false;
}

void inflight_remove(const char* id) {
    const int i = inflight_find(id);
    if (i >= 0) g.inflight_used[i] = false;
}

void handle_command(const RxItem& rx) {
    static CmdItem item;  // Decoded is large; keep it off the stack
    item.decoded = decode_command(rx.data, rx.len);  // only caller: not reentrant
    // Any inbound command proves the VMC is alive. VmcSeen goes first: the sequencer drains cmd_q
    // FIFO, and a Decoded ahead of it would be judged against a stale "silent" VMC.
    item.kind = CmdItem::VmcSeen;
    push_cmd(item);
    if (item.decoded.result == Decode::NoRequestId) {
        ++g.dropped_no_request_id;
        ESP_LOGW(TAG, "command without a usable request_id dropped (%u so far)", static_cast<unsigned>(g.dropped_no_request_id));
        return;
    }
    if (const Ack* hit = g.cache.find(item.decoded.cmd.request_id)) {
        publish_ack(*hit);  // retry of an acked command: replay, never re-run
        return;
    }
    if (inflight_find(item.decoded.cmd.request_id) >= 0) {
        ESP_LOGD(TAG, "duplicate of in-flight command %s ignored", item.decoded.cmd.request_id);
        return;
    }
    item.kind = CmdItem::Decoded;
    if (!push_cmd(item)) return;
    if (!inflight_add(item.decoded.cmd.request_id))
        ESP_LOGW(TAG, "in-flight set full; duplicate suppression off for %s", item.decoded.cmd.request_id);
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

// Retained capabilities and the HA discovery set; retried from the loop until all of it is accepted.
void publish_retained() {
    static char payload[4096];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    if (g.caps_pending) {
        const size_t n = capabilities_json(ctx, payload, sizeof(payload));
        char cap_tail[48];
        snprintf(cap_tail, sizeof(cap_tail), "capabilities/%s", kSubsystem);
        if (n == 0) {
            ESP_LOGW(TAG, "capabilities_json produced nothing; will retry");
            return;
        }
        if (topic_join(ctx, cap_tail, topic, sizeof(topic)) == 0 || !mqtt_publish(topic, payload, n, true)) {
            ESP_LOGW(TAG, "capabilities publish failed; will retry");
            return;
        }
        g.caps_pending = false;
    }
    while (g.ha_next < ha_discovery_count()) {
        const size_t pn = ha_discovery(g.ha_next, ctx, topic, sizeof(topic), payload, sizeof(payload));
        if (pn > 0 && !mqtt_publish(topic, payload, pn, true)) {
            ESP_LOGW(TAG, "HA discovery %d publish failed; will retry", g.ha_next);
            return;
        }
        ++g.ha_next;
    }
}

void subscribe_all() {
    bool ok = mqtt_subscribe(g.t_cmd);
    if (!mqtt_subscribe(g.t_online)) ok = false;
    if (!ok) ESP_LOGW(TAG, "subscribe failed; will retry");
    g.subs_pending = !ok;
}

void on_connect() {
    static char payload[96];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    g.caps_pending = true;
    g.ha_next = 0;
    publish_retained();
    char hb_tail[48];
    snprintf(hb_tail, sizeof(hb_tail), "heartbeat/%s", kSubsystem);
    const size_t n = heartbeat_json(uptime_s(), payload, sizeof(payload));
    if (n == 0 || topic_join(ctx, hb_tail, topic, sizeof(topic)) == 0) {
        ESP_LOGW(TAG, "connect heartbeat did not serialize");
    } else if (!mqtt_publish(topic, payload, n, false)) {
        ESP_LOGW(TAG, "connect heartbeat publish failed");
    }
    subscribe_all();
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
    uint32_t last_drop_log = 0;
    for (;;) {
        static RxItem rx;
        // ack_q first: a retry arriving now must find the first ack already cached.
        static Ack ack;
        while (xQueueReceive(g.ack_q, &ack, 0) == pdTRUE) {
            g.cache.put(ack);
            inflight_remove(ack.request_id);
            publish_ack(ack);
        }
        while (xQueueReceive(g.rx_q, &rx, 0) == pdTRUE) {
            if (strcmp(rx.topic, g.t_cmd) == 0) handle_command(rx);
            else if (strcmp(rx.topic, g.t_online) == 0) handle_online(rx);
        }
        if (mqtt_take_connected_edge()) {
            on_connect();
            last_hb = uptime_s();
        }
        const uint32_t dropped = g.rx_dropped.load();
        if (dropped != g.rx_dropped_logged && uptime_s() - last_drop_log >= kRxDropLogMs / 1000) {
            ESP_LOGW(TAG, "inbound messages dropped: %u total", static_cast<unsigned>(dropped));
            g.rx_dropped_logged = dropped;
            last_drop_log = uptime_s();
        }
        if (mqtt_connected()) {
            if (g.caps_pending || g.ha_next < ha_discovery_count()) publish_retained();
            if (g.subs_pending) subscribe_all();
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
