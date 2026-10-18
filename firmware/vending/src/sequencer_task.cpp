#include "sequencer_task.h"

#include "core.h"
#include "esp_clock.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "../boards/kc868-a8.h"
#include "kc868_io.h"
#include "polarity_setting.h"
#include "queues.h"

namespace {
const char* TAG = "sequencer";
constexpr TickType_t kPeriod = pdMS_TO_TICKS(10);

struct Args {
    QueueHandle_t cmd_q, evt_q, ack_q;
};

// One-slot holding buffer: an ack refused by a full ack_q is retried on the next tick; only a second
// consecutive refusal loses it (a first ack is what the edge's command timeout depends on).
vendcore::Ack g_held;
bool g_have_held = false;

void flush_held(QueueHandle_t q) {
    if (!g_have_held) return;
    if (xQueueSend(q, &g_held, 0) == pdTRUE) {
        g_have_held = false;
    }
}

void send_ack(QueueHandle_t q, const vendcore::Ack& a) {
    flush_held(q);
    if (g_have_held) {  // still stuck: the older ack goes first, so this one cannot be queued behind it
        ESP_LOGE(TAG, "ack queue full, dropped %s", a.request_id);
        return;
    }
    if (xQueueSend(q, &a, 0) != pdTRUE) {
        g_held = a;
        g_have_held = true;
        ESP_LOGW(TAG, "ack queue full, holding %s for retry", a.request_id);
    }
}

// submit() and tick() both run here, on this one task: the core has no locking.
void drain_commands(vendcore::Core& core, QueueHandle_t cmd_q, QueueHandle_t ack_q) {
    static CmdItem item;
    while (xQueueReceive(cmd_q, &item, 0) == pdTRUE) {
        switch (item.kind) {
            case CmdItem::Decoded: send_ack(ack_q, core.submit(item.decoded)); break;
            case CmdItem::VmcSeen: core.vmc_seen(); break;
            case CmdItem::VmcOnline: core.vmc_online(item.online); break;
            case CmdItem::ForceReport: core.force_report(); break;
        }
    }
}

void sequencer_main(void* arg) {
    static Args args = *static_cast<Args*>(arg);
    static Kc868Io io;
    static EspClock clock;
    static QueuePublisher pub(args.evt_q);
    if (io.begin() != ESP_OK) {
        // Fail-safe: never kick the external watchdog; reboot and retry.
        ESP_LOGE(TAG, "board I/O init failed; rebooting in 5 s");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }
    vendcore::CoreConfig cfg;
    uint8_t polarity[kPolarityCount];
    polarity_load(polarity);
    for (int i = 0; i < vendcore::kBinaryInputCount; ++i) {
        cfg.polarity.active_low[i] = polarity[i] != 0;
    }
    static vendcore::Core core(cfg, clock, io, pub);

    esp_task_wdt_add(nullptr);
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        flush_held(args.ack_q);
        drain_commands(core, args.cmd_q, args.ack_q);
        core.tick();
        vendcore::Ack done;
        while (core.poll_completion(done)) send_ack(args.ack_q, done);
        esp_task_wdt_reset();
        vTaskDelayUntil(&last, kPeriod);
    }
}
}  // namespace

void start_sequencer(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q) {
    static Args args;
    args = Args{cmd_q, evt_q, ack_q};
    xTaskCreatePinnedToCore(sequencer_main, "sequencer", 8192, &args, 20, nullptr, 1);
}
