#include "ack_cache.h"
#include "comms_task.h"
#include "console.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "ports.h"
#include "queues.h"
#include "sequencer_task.h"
#include "settings.h"
#include "version.h"

static const char* TAG = "vending";

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "firmware %s", vendcore::kFirmwareVersion);
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    static Settings settings;
    settings_load(settings);
    console_start(settings);

    QueueHandle_t cmd_q = xQueueCreate(8, sizeof(CmdItem));
    QueueHandle_t evt_q = xQueueCreate(64, sizeof(vendcore::Event));
    QueueHandle_t ack_q = xQueueCreate(8, sizeof(vendcore::Ack));
    start_sequencer(cmd_q, evt_q, ack_q, settings);
    start_comms(cmd_q, evt_q, ack_q, settings);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
