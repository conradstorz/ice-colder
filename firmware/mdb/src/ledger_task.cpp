#include "ledger_task.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include <atomic>

#include "boardidf/net.h"
#include "esp_ids.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_log_store.h"
#include "status_led.h"
#include "wire.h"

using namespace mdbcore;

namespace {
const char* TAG = "ledger";
constexpr uint32_t kErrLogMs = 10000;
constexpr int kAppendsPerPass = 8;

Queues g_q;
NvsLogStore g_store;
EspIds g_ids;
EventLog g_log(g_store, g_ids);
bool g_store_ok = false;

char g_topic_event[96];
char g_topic_state[96];

char g_payouts_boot[kPayoutsMax];
size_t g_payouts_boot_len = 0;
char g_payouts_saved[kPayoutsMax];
size_t g_payouts_saved_len = 0;

std::atomic<bool> g_connect_edge{false};

// Replay cursor: index of the next log entry to hand to comms, or -1 when not replaying.
int g_replay_next = -1;

// An event whose append the store refused (log full or flash error): held, retried, never dropped.
PaymentEvent g_held_evt;
bool g_have_evt = false;
// An envelope already in the log but not yet accepted by pub_q.
char g_env[LogStore::kMaxJson];
size_t g_env_len = 0;
bool g_have_env = false;

StateMsg g_last;
bool g_have_state = false;
bool g_state_dirty = false;

uint32_t g_last_err_log_ms = 0;
bool g_err_logged = false;

uint32_t now_ms() { return static_cast<uint32_t>(xTaskGetTickCount() * portTICK_PERIOD_MS); }

bool err_log_due() {
    const uint32_t now = now_ms();
    if (g_err_logged && now - g_last_err_log_ms < kErrLogMs) return false;
    g_err_logged = true;
    g_last_err_log_ms = now;
    return true;
}

// SNTP time when synced; otherwise the boot clock as seconds since 1970 ("best effort", spec section 6).
void iso_now(char* out, size_t cap) {
    if (net_iso_time(out, cap)) return;
    const time_t t = time(nullptr);
    struct tm tm_utc;
    gmtime_r(&t, &tm_utc);
    strftime(out, cap, "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
}

bool push_pub(const char* topic, const char* payload, size_t len, bool retain, boardcore::Prio prio) {
    static PubItem item;
    if (len >= sizeof(item.payload) || strlen(topic) >= sizeof(item.topic)) return true;  // cannot ever fit: drop
    if (uxQueueSpacesAvailable(g_q.pub_q) == 0) return false;
    snprintf(item.topic, sizeof(item.topic), "%s", topic);
    memcpy(item.payload, payload, len);
    item.payload[len] = '\0';
    item.len = static_cast<uint16_t>(len);
    item.retain = retain;
    item.prio = prio;
    return xQueueSend(g_q.pub_q, &item, 0) == pdTRUE;
}

void handle_acks() {
    static EventAckItem a;
    while (xQueueReceive(g_q.ack_in_q, &a, 0) == pdTRUE) {
        const int before = g_log.pending();
        g_log.acked(a.boot_id, a.seq);
        const int erased = before - g_log.pending();
        if (g_replay_next >= 0) g_replay_next = g_replay_next > erased ? g_replay_next - erased : 0;
    }
}

void run_replay() {
    static char entry[LogStore::kMaxJson];
    while (g_replay_next >= 0) {
        if (g_replay_next >= g_log.pending()) {
            g_replay_next = -1;
            ESP_LOGI(TAG, "replay handed to comms");
            return;
        }
        if (uxQueueSpacesAvailable(g_q.pub_q) == 0) return;
        if (g_log.pending_at(g_replay_next, entry, sizeof(entry))) {
            push_pub(g_topic_event, entry, strlen(entry), false, boardcore::Prio::Outcome);
        } else {
            ESP_LOGE(TAG, "log entry %d unreadable; skipped in replay", g_replay_next);
        }
        ++g_replay_next;
    }
}

void drain_events() {
    static PaymentEvent evt;
    char iso[24];
    for (int n = 0; n < kAppendsPerPass; ++n) {
        if (g_have_env) {  // the previous envelope must reach pub_q first, so order is the log's order
            if (!push_pub(g_topic_event, g_env, g_env_len, false, boardcore::Prio::Outcome)) return;
            g_have_env = false;
        }
        if (g_have_evt) {
            evt = g_held_evt;
        } else if (xQueueReceive(g_q.evt_q, &evt, 0) != pdTRUE) {
            return;
        }
        iso_now(iso, sizeof(iso));
        if (!g_log.append(evt, iso, g_env, sizeof(g_env))) {
            g_held_evt = evt;
            g_have_evt = true;
            if (err_log_due())
                ESP_LOGE(TAG, "event %s not logged (%s); holding it, bus polling will defer",
                         event_kind_str(evt.kind), g_log.full() ? "replay log full" : "store refused");
            return;
        }
        g_have_evt = false;
        g_env_len = strlen(g_env);
        g_have_env = true;
    }
}

void handle_state() {
    static StateMsg msg;
    if (xQueueReceive(g_q.state_q, &msg, 0) == pdTRUE) {
        g_last = msg;
        g_last.state.policy_id = g_last.state.policy_id != nullptr ? g_last.policy_id : nullptr;
        g_last.state.session_started_ts = g_last.state.session_started_ts != nullptr ? g_last.session_ts : nullptr;
        g_have_state = true;
        g_state_dirty = true;
        bool offline = false;
        for (const DeviceState& d : g_last.state.devices) offline = offline || (d.present && d.status == Status::Offline);
        status_led_set_state(g_last.state.inhibited == Inhibit::None, offline);
        if (g_last.payouts_len > 0 &&
            (g_last.payouts_len != g_payouts_saved_len || memcmp(g_last.payouts, g_payouts_saved, g_last.payouts_len) != 0)) {
            if (g_store.save_payouts(g_last.payouts, g_last.payouts_len)) {
                memcpy(g_payouts_saved, g_last.payouts, g_last.payouts_len);
                g_payouts_saved_len = g_last.payouts_len;
            }
        }
    }
    if (g_state_dirty && g_have_state && uxQueueSpacesAvailable(g_q.pub_q) > 0) {
        static char json[kPubPayloadMax];
        char iso[24];
        iso_now(iso, sizeof(iso));
        PaymentState st = g_last.state;
        st.boot_id = g_log.boot_id();
        st.seq_last = g_log.seq_last();
        const size_t n = state_json(st, iso, json, sizeof(json));
        g_state_dirty = false;  // a failure to serialize is not retried into a loop; the next change tries again
        if (n == 0) {
            ESP_LOGE(TAG, "payment/state did not serialize");
        } else {
            push_pub(g_topic_state, json, n, true, boardcore::Prio::Normal);
        }
    }
}

void ledger_main(void*) {
    for (;;) {
        if (g_connect_edge.exchange(false)) {
            g_replay_next = 0;
            g_have_env = false;  // already in the log, so the replay carries it
            g_state_dirty = true;
        }
        handle_acks();
        run_replay();
        if (g_replay_next < 0) drain_events();
        handle_state();
        status_led_set_log_full(g_log.full());
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
}  // namespace

bool ledger_init(const Queues& q, const char* machine_id) {
    g_q = q;
    WireCtx ctx;
    ctx.machine_id = machine_id;
    topic_event(ctx, g_topic_event, sizeof(g_topic_event));
    topic_state(ctx, g_topic_state, sizeof(g_topic_state));
    g_store_ok = g_store.begin();
    g_log.begin_boot();
    if (g_store_ok) {
        size_t len = 0;
        if (g_store.load_payouts(g_payouts_boot, sizeof(g_payouts_boot), len)) {
            g_payouts_boot_len = len;
            memcpy(g_payouts_saved, g_payouts_boot, len);
            g_payouts_saved_len = len;
        }
    }
    ESP_LOGI(TAG, "boot_id %s, %d unacked events in the log", g_log.boot_id(), g_log.pending());
    return g_store_ok;
}

void start_ledger() { xTaskCreatePinnedToCore(ledger_main, "ledger", 8192, nullptr, 10, nullptr, 0); }

const EventLog& ledger_event_log() { return g_log; }

const char* ledger_payouts_blob(size_t& len) {
    len = g_payouts_boot_len;
    return g_payouts_boot_len > 0 ? g_payouts_boot : nullptr;
}

void ledger_connected() { g_connect_edge.store(true); }
