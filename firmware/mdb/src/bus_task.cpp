#include "bus_task.h"

#include <stdio.h>
#include <string.h>

#include "../boards/mdb-s3.h"
#include "accounts.h"
#include "cashless.h"
#include "changer.h"
#include "command.h"
#include "edge_watch.h"
#include "esp_clock.h"
#include "esp_ids.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ledger_task.h"
#include "link.h"
#include "master.h"
#include "uart_bus.h"
#include "validator.h"

using namespace mdbcore;
using boardcore::Ack;
using boardcore::AckStatus;
using boardcore::Phase;

namespace {
const char* TAG = "bus";
constexpr TickType_t kLoopDelay = 2;           // ticks (ms) between bus rounds; lets IDLE1 run
constexpr uint32_t kStatePushMs = 30000;
constexpr uint32_t kStatsLogMs = 60000;

// emit() refusing makes the device driver hold the money event and offer it again next step.
struct QueueSink : EventSink {
    QueueHandle_t q = nullptr;
    bool emit(const PaymentEvent& e) override { return xQueueSend(q, &e, 0) == pdTRUE; }
};

// One-slot hold for an ack refused by a full ack_q (the first ack is what the edge's command
// timeout depends on): retried every round; only a second consecutive refusal loses one.
Ack g_held;
bool g_have_held = false;

void flush_held(QueueHandle_t q) {
    if (g_have_held && xQueueSend(q, &g_held, 0) == pdTRUE) g_have_held = false;
}

void send_ack(QueueHandle_t q, const Ack& a) {
    flush_held(q);
    if (g_have_held) {
        ESP_LOGE(TAG, "ack queue full, dropped %s", a.request_id);
        return;
    }
    if (xQueueSend(q, &a, 0) != pdTRUE) {
        g_held = a;
        g_have_held = true;
        ESP_LOGW(TAG, "ack queue full, holding %s for retry", a.request_id);
    }
}

void copy_str(char* dst, size_t cap, const char* src) {
    snprintf(dst, cap, "%s", src);
}

Ack refusal_ack(const Decoded& d) {
    Ack a = {};
    copy_str(a.request_id, sizeof(a.request_id), d.cmd.request_id);
    copy_str(a.command, sizeof(a.command), d.cmd.name);
    a.status = d.result == Decode::Unsupported ? AckStatus::Unsupported : AckStatus::Rejected;
    a.phase = Phase::Completed;
    copy_str(a.detail, sizeof(a.detail), d.detail);
    return a;
}

struct Bus {
    Queues q;
    EspClock clock;
    EspIds ids;
    UartBus uart;
    QueueSink sink;
    Link link{uart, clock};
    MasterTap tap;
    Changer changer{link, clock, tap};
    Validator validator{link, clock, tap, tap};
    Cashless cashless{link, clock, tap, ids};
    Accounts accounts;
    EdgeWatch edge{clock};
    Master master{link, clock, ids, sink, changer, validator, cashless, accounts, edge};
};
void push_state(Bus& b) {
    static StateMsg msg;
    b.master.snapshot(msg.state);
    msg.state.boot_id = nullptr;  // the ledger owns the EventLog and fills boot_id and seq_last
    msg.state.seq_last = 0;
    if (msg.state.policy_id != nullptr) {
        copy_str(msg.policy_id, sizeof(msg.policy_id), msg.state.policy_id);
        msg.state.policy_id = msg.policy_id;
    }
    if (msg.state.session_started_ts != nullptr) {
        copy_str(msg.session_ts, sizeof(msg.session_ts), msg.state.session_started_ts);
        msg.state.session_started_ts = msg.session_ts;
    }
    msg.payouts_len = b.accounts.payouts_json(msg.payouts, sizeof(msg.payouts));
    if (msg.payouts_len == 0) ESP_LOGE(TAG, "payouts store did not serialize (%u B buffer)", static_cast<unsigned>(sizeof(msg.payouts)));
    xQueueOverwrite(b.q.state_q, &msg);
}

void drain_commands(Bus& b) {
    static CmdItem item;
    while (xQueueReceive(b.q.cmd_q, &item, 0) == pdTRUE) {
        switch (item.kind) {
            case CmdItem::Decoded:
                if (item.decoded.result == Decode::Ok) send_ack(b.q.ack_q, b.master.submit(item.decoded.cmd));
                else send_ack(b.q.ack_q, refusal_ack(item.decoded));
                break;
            case CmdItem::EdgeSeen: b.master.edge_seen(); break;
            case CmdItem::ForceReport: b.master.force_report(); break;
            case CmdItem::WallTime: b.master.set_wall_time(item.wall_unix); break;
        }
    }
}

void bus_main(void* arg) {
    static Bus bus;
    Bus& b = bus;
    b.q = *static_cast<Queues*>(arg);
    b.sink.q = b.q.evt_q;
    if (!b.uart.begin(mdbboard::kMdbUart, mdbboard::kMdbTxGpio, mdbboard::kMdbRxGpio)) {
        // Fail-safe: never kick the watchdog on a dead bus; reboot and retry.
        ESP_LOGE(TAG, "MDB UART init failed; rebooting in 5 s");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }
    b.tap.bind(b.master);
    b.master.set_event_log(&ledger_event_log());
    size_t plen = 0;
    const char* payouts = ledger_payouts_blob(plen);
    if (payouts != nullptr) b.accounts.restore_payouts(payouts, plen);

    esp_task_wdt_add(nullptr);
    uint32_t last_push = b.clock.now_ms();
    uint32_t last_stats = last_push;
    push_state(b);  // retained "no_policy" view until the edge's first set_policy
    for (;;) {
        flush_held(b.q.ack_q);
        drain_commands(b);
        b.master.step();
        Ack done;
        while (b.master.poll_completion(done)) send_ack(b.q.ack_q, done);
        const uint32_t now = b.clock.now_ms();
        if (b.master.take_state_dirty() || now - last_push >= kStatePushMs) {
            last_push = now;
            push_state(b);
        }
        if (now - last_stats >= kStatsLogMs) {
            last_stats = now;
            if (b.link.mode_disagreements() != 0 || b.uart.ambiguous() != 0 || b.uart.orphans() != 0 || b.uart.rx_lost() != 0 ||
                b.link.corrupt_blocks() != 0) {
                ESP_LOGI(TAG, "rx stats: mode disagreements %u, ambiguous %u, orphan parity %u, lost %u, corrupt blocks %u",
                         static_cast<unsigned>(b.link.mode_disagreements()), static_cast<unsigned>(b.uart.ambiguous()),
                         static_cast<unsigned>(b.uart.orphans()), static_cast<unsigned>(b.uart.rx_lost()),
                         static_cast<unsigned>(b.link.corrupt_blocks()));
            }
        }
        esp_task_wdt_reset();
        vTaskDelay(kLoopDelay);
    }
}
}  // namespace

void start_bus(const Queues& q) {
    static Queues args;
    args = q;
    xTaskCreatePinnedToCore(bus_main, "bus", 12288, &args, 20, nullptr, 1);
}
