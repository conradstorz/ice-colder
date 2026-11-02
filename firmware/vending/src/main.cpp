#include "boardcore/ack_cache.h"
#include "comms_task.h"
#include "boardidf/board_identity.h"
#include "boardidf/boot.h"
#include "boardidf/settings.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "ports.h"
#include "queues.h"
#include "sequencer_task.h"
#include "polarity_setting.h"
#include "version.h"

static const char* TAG = "vending";

static const BoardIdentity kIdentity = {"vending", "vend> ", "vend", true};

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "firmware %s", vendcore::kFirmwareVersion);
    static Settings settings;
    polarity_register();
    boardidf::board_boot(kIdentity, settings, nullptr);

    QueueHandle_t cmd_q = xQueueCreate(8, sizeof(CmdItem));
    QueueHandle_t evt_q = xQueueCreate(128, sizeof(vendcore::Event));
    QueueHandle_t ack_q = xQueueCreate(16, sizeof(vendcore::Ack));
    start_sequencer(cmd_q, evt_q, ack_q);
    start_comms(cmd_q, evt_q, ack_q, kIdentity, settings);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
