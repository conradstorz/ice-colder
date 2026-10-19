#include "comms_task.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include <atomic>

#include "boardcore/ack_cache.h"
#include "boardcore/json_writer.h"
#include "boardcore/outbound_ring.h"
#include "boardidf/mqtt_publisher.h"
#include "boardidf/net.h"
#include "command.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ledger_task.h"
#include "version.h"
#include "wire.h"

using namespace mdbcore;
using boardcore::Ack;
using boardcore::AckCache;
using boardcore::JsonWriter;
using boardcore::OutboundRing;
using boardcore::Prio;
using boardcore::RingEntry;

namespace {
const char* TAG = "comms";
constexpr uint32_t kHeartbeatMs = 10000;
constexpr uint32_t kWallRefreshS = 60;
constexpr int kInflightMax = 8;
constexpr uint32_t kRxDropLogMs = 10000;
constexpr uint32_t kRetryLogMs = 10000;
constexpr uint32_t kInflightMaxAgeS = 120;
constexpr uint32_t kReplayRequestMinS = 5;
constexpr int kHaCount = 6;
constexpr int kAckSlots = 4;
constexpr int kRxDepth = 8;

struct RxItem {
    char topic[96];
    char data[2048];
    uint16_t len;
};

// payment/event/ack is cumulative per boot_id, so a burst (an old-boot backlog being re-acked during
// a replay) collapses to one pending value per boot: the MQTT callback keeps the maximum seq per
// boot_id here, and the comms loop drains it into ack_in_q. An ack storm therefore can neither
// overflow rx_q (acks never enter it) nor wedge the command path behind them.
struct AckSlot {
    bool used;
    char boot_id[33];
    uint32_t seq;
};

struct Comms {
    Queues q;
    AckSlot acks[kAckSlots] = {};
    QueueHandle_t rx_q;
    Settings settings;
    AckCache cache;
    OutboundRing ring;
    uint32_t dropped_no_request_id = 0;
    std::atomic<uint32_t> rx_dropped{0};  // written by the MQTT task, logged by the comms task
    uint32_t rx_dropped_logged = 0;
    // Request ids pushed to cmd_q whose first ack has not yet been drained from ack_q.
    char inflight[kInflightMax][65] = {};
    bool inflight_used[kInflightMax] = {};
    uint32_t inflight_since_s[kInflightMax] = {};
    uint32_t last_retry_log_s = 0;
    bool retry_logged = false;
    int ha_zero_logged = -1;
    bool caps_pending = false;  // retained capabilities publish not yet accepted
    bool subs_pending = false;
    int ha_next = 0;
    uint32_t last_replay_request_s = 0;
    bool replay_requested = false;
    // Replay ordering (CONTRACT.md Delivery: the whole unacked log, in order, before anything new).
    //  synced:      the connect edge has been processed (queue drained, replay requested). Until then,
    //               and while disconnected, Outcome items are dropped: the flash log carries them.
    //  replay_owed: an Outcome was lost while connected (ring full), so a gap exists. Every later
    //               Outcome is dropped, whatever its epoch, until a replay is requested; the replay
    //               resends the whole log in order.
    bool synced = false;
    bool replay_owed = false;
    uint32_t stale_dropped = 0;
    uint32_t pub_dropped = 0;
    char t_cmd[96], t_ack[96], t_evt_ack[96];
};
Comms g;
portMUX_TYPE g_ack_mux = portMUX_INITIALIZER_UNLOCKED;

uint32_t uptime_s() { return static_cast<uint32_t>(esp_timer_get_time() / 1000000); }

// Connect-time retry warnings repeat every 20 ms loop pass; allow one per kRetryLogMs.
bool retry_log_due() {
    const uint32_t now = uptime_s();
    if (g.retry_logged && now - g.last_retry_log_s < kRetryLogMs / 1000) return false;
    g.retry_logged = true;
    g.last_retry_log_s = now;
    return true;
}

WireCtx make_ctx(char* iso, size_t iso_cap) {
    WireCtx c;
    c.machine_id = g.settings.machine_id;
    c.hardware_id = net_hardware_id();
    c.ip = net_ip();
    c.firmware = kFirmwareVersion;
    c.iso_time = net_iso_time(iso, iso_cap) ? iso : nullptr;
    return c;
}

void clear_ring() {
    while (g.ring.front() != nullptr) g.ring.pop();
}

// An event the ring could not hold is still safe in the flash log; have the ledger replay it.
// Rate limited (the latch g.replay_owed keeps asking from the main loop); clears the ring first so
// the replay stays oldest first. ledger_connected() bumps the replay epoch, which makes every
// Outcome already queued behind us stale.
void request_replay() {
    const uint32_t now = uptime_s();
    if (g.replay_requested && now - g.last_replay_request_s < kReplayRequestMinS) return;
    g.replay_requested = true;
    g.last_replay_request_s = now;
    clear_ring();
    ledger_connected();
    g.replay_owed = false;
}

// Publishes now when connected and nothing is queued ahead; otherwise holds it in the ring.
// Money events (Outcome) that cannot be published or held fall back to the flash log's replay.
void send(const char* topic, const char* payload, size_t len, bool retain, Prio prio) {
    // The log replays an Outcome dropped here: before the connect edge was processed, while
    // disconnected, or after an earlier Outcome was lost (a later one must not overtake the gap).
    if (prio == Prio::Outcome && (!g.synced || g.replay_owed)) return;
    if (g.ring.size() == 0 && mqtt_publish(topic, payload, len, retain)) return;
    if (prio == Prio::Outcome && !mqtt_connected()) return;
    // Ring rule: OutboundRing::push evicts the oldest Outcome when the ring is all Outcomes and
    // reports success, which would silently lose a money event while later ones overtake it (the
    // edge acks cumulatively). So a money event never pushes into a full ring: that is a drop.
    if (prio == Prio::Outcome && g.ring.full()) {
        ++g.pub_dropped;
        g.replay_owed = true;
        request_replay();
        return;
    }
    if (!g.ring.push(topic, payload, len, retain, prio)) {
        ++g.pub_dropped;
        if (prio == Prio::Outcome) {
            g.replay_owed = true;
            request_replay();
        }
    }
}

void flush_ring() {
    while (const RingEntry* e = g.ring.front()) {
        if (!mqtt_publish(e->topic, e->payload, e->len, e->retain)) return;
        g.ring.pop();
    }
}

// Keeps the highest seq per boot_id. A fifth boot evicts the slot holding the lowest seq (the least
// information: acks are cumulative, and the edge re-acks on the next replay). Short critical section.
void coalesce_ack(const char* boot_id, uint32_t seq) {
    portENTER_CRITICAL(&g_ack_mux);
    int hit = -1, free_slot = -1, lowest = 0;
    for (int i = 0; i < kAckSlots; ++i) {
        if (!g.acks[i].used) {
            if (free_slot < 0) free_slot = i;
            continue;
        }
        if (strcmp(g.acks[i].boot_id, boot_id) == 0) hit = i;
        if (g.acks[i].seq < g.acks[lowest].seq || !g.acks[lowest].used) lowest = i;
    }
    if (hit >= 0) {
        if (seq > g.acks[hit].seq) g.acks[hit].seq = seq;
    } else {
        const int slot = free_slot >= 0 ? free_slot : lowest;
        g.acks[slot].used = true;
        memcpy(g.acks[slot].boot_id, boot_id, strlen(boot_id) + 1);
        g.acks[slot].seq = seq;
    }
    portEXIT_CRITICAL(&g_ack_mux);
}

// Comms loop: moves the coalesced acks to the ledger. One that the ledger queue refuses goes back
// into the table (merged with anything newer) and is retried on the next pass.
void drain_acks() {
    for (int i = 0; i < kAckSlots; ++i) {
        EventAckItem item;
        bool have = false;
        portENTER_CRITICAL(&g_ack_mux);
        if (g.acks[i].used) {
            memcpy(item.boot_id, g.acks[i].boot_id, sizeof(item.boot_id));
            item.seq = g.acks[i].seq;
            g.acks[i].used = false;
            have = true;
        }
        portEXIT_CRITICAL(&g_ack_mux);
        if (!have) continue;
        if (xQueueSend(g.q.ack_in_q, &item, 0) != pdTRUE) {
            // Stop at the first refusal: a later slot must not overtake this (older) one.
            coalesce_ack(item.boot_id, item.seq);
            break;
        }
    }
}

void on_message(const char* topic, size_t tl, const char* data, size_t dl) {  // MQTT client task
    if (tl == strlen(g.t_evt_ack) && memcmp(topic, g.t_evt_ack, tl) == 0) {
        char boot_id[33];
        uint32_t seq;
        if (decode_event_ack(data, dl, boot_id, seq)) coalesce_ack(boot_id, seq);
        return;  // malformed acks are ignored: the edge re-acks on the next replay
    }
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
    static char buf[1024];
    const size_t n = boardcore::ack_json(a, buf, sizeof(buf));
    if (n == 0) {
        ESP_LOGW(TAG, "ack for %s did not serialize", a.request_id);
        return;
    }
    send(g.t_ack, buf, n, false, Prio::Normal);
}

bool push_cmd(const CmdItem& item) {
    if (xQueueSend(g.q.cmd_q, &item, 0) == pdTRUE) return true;
    ESP_LOGW(TAG, "cmd queue full, dropped item kind %d", static_cast<int>(item.kind));
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
        g.inflight_since_s[i] = uptime_s();
        return true;
    }
    return false;
}

// An ack lost to a full ack_q would otherwise pin its slot forever, so the set is cleared on every
// (re)connect and any entry older than kInflightMaxAgeS is dropped.
void inflight_clear() {
    for (int i = 0; i < kInflightMax; ++i) g.inflight_used[i] = false;
}

void inflight_expire() {
    const uint32_t now = uptime_s();
    for (int i = 0; i < kInflightMax; ++i)
        if (g.inflight_used[i] && now - g.inflight_since_s[i] >= kInflightMaxAgeS) g.inflight_used[i] = false;
}

void inflight_remove(const char* id) {
    const int i = inflight_find(id);
    if (i >= 0) g.inflight_used[i] = false;
}

void publish_heartbeat() {
    static char buf[96];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    char tail[48];
    snprintf(tail, sizeof(tail), "heartbeat/%s", kSubsystem);
    const size_t n = heartbeat_json(uptime_s(), buf, sizeof(buf));
    if (n > 0 && topic_join(ctx, tail, topic, sizeof(topic)) > 0) send(topic, buf, n, false, Prio::Normal);
}

void handle_command(const RxItem& rx) {
    static CmdItem item;  // Decoded is large; keep it off the stack
    item = CmdItem{};
    // Any cmd/mdb message proves the edge is alive, whatever it asks. EdgeSeen goes first: the bus
    // task drains cmd_q FIFO. payment/event/ack never reaches here, so it never counts (spec 3.2).
    item.kind = CmdItem::EdgeSeen;
    push_cmd(item);
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
    if (inflight_find(item.decoded.cmd.request_id) >= 0) {
        ESP_LOGD(TAG, "duplicate of in-flight command %s ignored", item.decoded.cmd.request_id);
        return;
    }
    item.kind = CmdItem::Decoded;
    if (!push_cmd(item)) return;
    if (!inflight_add(item.decoded.cmd.request_id))
        ESP_LOGW(TAG, "in-flight set full; duplicate suppression off for %s", item.decoded.cmd.request_id);
    if (item.decoded.result == Decode::Ok && item.decoded.cmd.kind == CmdKind::ForceReport) {
        // force_report also refreshes the retained documents and the heartbeat, not just the state.
        g.caps_pending = true;
        g.ha_next = 0;
        g.ha_zero_logged = -1;
        publish_heartbeat();
    }
}

// One Home Assistant discovery entity. Returns bytes written to payload, 0 on overflow.
size_t ha_discovery(int i, const WireCtx& ctx, char* topic, size_t tcap, char* payload, size_t pcap) {
    struct Entity {
        const char* id;
        const char* name;
        const char* component;
        const char* tmpl;
        const char* unit;
    };
    static const Entity kEntities[kHaCount] = {
        {"escrow_cents", "Cash Escrow", "sensor", "{{ value_json.escrow_cents }}", "cents"},
        {"accepting", "Accepting Cash", "binary_sensor", "{{ 'ON' if value_json.accepting else 'OFF' }}", nullptr},
        {"exact_change", "Exact Change Only", "binary_sensor", "{{ 'ON' if value_json.exact_change else 'OFF' }}", nullptr},
        {"coin_status", "Coin Changer", "sensor", "{{ value_json.devices.coin.status }}", nullptr},
        {"bill_status", "Bill Validator", "sensor", "{{ value_json.devices.bill.status }}", nullptr},
        {"cashless_status", "Cashless Reader", "sensor", "{{ value_json.devices.cashless.status }}", nullptr},
    };
    if (i < 0 || i >= kHaCount) return 0;
    const Entity& e = kEntities[i];
    char node[80];
    snprintf(node, sizeof(node), "%s_%s", ctx.machine_id, kSubsystem);
    const int tn = snprintf(topic, tcap, "homeassistant/%s/%s/%s/config", e.component, node, e.id);
    if (tn <= 0 || static_cast<size_t>(tn) >= tcap) return 0;
    char uid[112];
    snprintf(uid, sizeof(uid), "%s_%s", node, e.id);
    char state_topic[128];
    topic_state(ctx, state_topic, sizeof(state_topic));
    char dev_name[80];
    snprintf(dev_name, sizeof(dev_name), "%s Payments", ctx.machine_id);
    JsonWriter w(payload, pcap);
    w.ch('{');
    w.field("name", e.name);
    w.ch(',');
    w.field("unique_id", uid);
    w.ch(',');
    w.field("state_topic", state_topic);
    w.ch(',');
    w.field("value_template", e.tmpl);
    if (e.unit != nullptr) {
        w.ch(',');
        w.field("unit_of_measurement", e.unit);
    }
    w.ch(',');
    w.key("device");
    w.ch('{');
    w.key("identifiers");
    w.ch('[');
    w.str(node);
    w.ch(']');
    w.ch(',');
    w.field("name", dev_name);
    w.ch(',');
    w.field("manufacturer", "ice-colder");
    w.ch(',');
    w.field("model", "ESP32-S3 MDB");
    w.ch('}');
    w.ch('}');
    return w.done();
}

// Retained capabilities and the HA discovery set; retried from the loop until all of it is accepted.
void publish_retained() {
    static char payload[1024];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    if (g.caps_pending) {
        const size_t n = capabilities_json(ctx, payload, sizeof(payload));
        char cap_tail[48];
        snprintf(cap_tail, sizeof(cap_tail), "capabilities/%s", kSubsystem);
        if (n == 0) {
            if (retry_log_due()) ESP_LOGW(TAG, "capabilities_json produced nothing; will retry");
            return;
        }
        if (topic_join(ctx, cap_tail, topic, sizeof(topic)) == 0 || !mqtt_publish(topic, payload, n, true)) {
            if (retry_log_due()) ESP_LOGW(TAG, "capabilities publish failed; will retry");
            return;
        }
        g.caps_pending = false;
    }
    while (g.ha_next < kHaCount) {
        const size_t pn = ha_discovery(g.ha_next, ctx, topic, sizeof(topic), payload, sizeof(payload));
        if (pn == 0 && g.ha_zero_logged != g.ha_next) {
            g.ha_zero_logged = g.ha_next;
            ESP_LOGW(TAG, "HA discovery %d produced nothing; skipped", g.ha_next);
        }
        if (pn > 0 && !mqtt_publish(topic, payload, pn, true)) {
            if (retry_log_due()) ESP_LOGW(TAG, "HA discovery %d publish failed; will retry", g.ha_next);
            return;
        }
        ++g.ha_next;
    }
}

void subscribe_all() {
    bool ok = mqtt_subscribe(g.t_cmd);
    if (!mqtt_subscribe(g.t_evt_ack)) ok = false;
    if (!ok && retry_log_due()) ESP_LOGW(TAG, "subscribe failed; will retry");
    g.subs_pending = !ok;
}

void on_connect() {
    static char payload[96];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    // Anything held across the gap is stale; events come back from the log, in order. Drain pub_q and
    // the ring BEFORE requesting the replay: what the ledger queues after that carries the new epoch.
    // Items it queued before it saw the request carry the old epoch and are dropped by the main loop.
    static PubItem stale;
    while (xQueueReceive(g.q.pub_q, &stale, 0) == pdTRUE) {
    }
    clear_ring();
    inflight_clear();
    g.replay_owed = false;
    g.replay_requested = false;
    ledger_connected();
    g.synced = true;
    g.caps_pending = true;
    g.ha_next = 0;
    g.ha_zero_logged = -1;
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
    static CmdItem fr;
    fr = CmdItem{};
    fr.kind = CmdItem::ForceReport;
    push_cmd(fr);
}

void send_wall_time(bool& sent, uint32_t& last_s) {
    char iso[24];
    if (!net_iso_time(iso, sizeof(iso))) return;
    if (sent && uptime_s() - last_s < kWallRefreshS) return;
    static CmdItem item;
    item = CmdItem{};
    item.kind = CmdItem::WallTime;
    item.wall_unix = static_cast<int64_t>(time(nullptr));
    if (push_cmd(item)) {
        sent = true;
        last_s = uptime_s();
    }
}

void comms_main(void*) {
    uint32_t last_hb = 0;
    uint32_t last_drop_log = 0;
    uint32_t last_wall_s = 0;
    bool wall_sent = false;
    for (;;) {
        static RxItem rx;
        // ack_q first: a retry arriving now must find the first ack already cached.
        static Ack ack;
        while (xQueueReceive(g.q.ack_q, &ack, 0) == pdTRUE) {
            g.cache.put(ack);
            inflight_remove(ack.request_id);
            publish_ack(ack);
        }
        while (xQueueReceive(g.rx_q, &rx, 0) == pdTRUE) {
            if (strcmp(rx.topic, g.t_cmd) == 0) handle_command(rx);
        }
        drain_acks();
        if (mqtt_take_connected_edge()) {
            on_connect();
            last_hb = uptime_s();
        }
        if (!mqtt_connected()) g.synced = false;
        if (g.replay_owed) request_replay();
        inflight_expire();
        send_wall_time(wall_sent, last_wall_s);
        const uint32_t dropped = g.rx_dropped.load();
        if (dropped != g.rx_dropped_logged && uptime_s() - last_drop_log >= kRxDropLogMs / 1000) {
            ESP_LOGW(TAG, "inbound messages dropped: %u total", static_cast<unsigned>(dropped));
            g.rx_dropped_logged = dropped;
            last_drop_log = uptime_s();
        }
        if (mqtt_connected()) {
            if (g.caps_pending || g.ha_next < kHaCount) publish_retained();
            if (g.subs_pending) subscribe_all();
        }
        static PubItem pub;
        while (xQueueReceive(g.q.pub_q, &pub, 0) == pdTRUE) {
            if (pub.prio == Prio::Outcome && pub.replay_epoch != ledger_replay_epoch()) {
                ++g.stale_dropped;  // queued before the latest replay request: the replay resends it in order
                continue;
            }
            send(pub.topic, pub.payload, pub.len, pub.retain, pub.prio);
        }
        if (mqtt_connected()) {
            flush_ring();
            if (uptime_s() - last_hb >= kHeartbeatMs / 1000) {
                last_hb = uptime_s();
                publish_heartbeat();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
}  // namespace

void start_comms(const Queues& q, const BoardIdentity& id, const Settings& settings) {
    g.q = q;
    g.rx_q = xQueueCreate(kRxDepth, sizeof(RxItem));
    g.settings = settings;
    WireCtx ctx;
    ctx.machine_id = g.settings.machine_id;
    topic_cmd(ctx, g.t_cmd, sizeof(g.t_cmd));
    topic_ack(ctx, g.t_ack, sizeof(g.t_ack));
    topic_event_ack(ctx, g.t_evt_ack, sizeof(g.t_evt_ack));
    net_start(id, settings);
    mqtt_start(id, settings, on_message);
    xTaskCreatePinnedToCore(comms_main, "comms", 8192, nullptr, 5, nullptr, 0);
}
