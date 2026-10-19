#include "boardidf/board_identity.h"
#include "boardidf/console.h"
#include "boardidf/settings.h"
#include "comms_task.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "version.h"

static const char* TAG = "mdb";

static const BoardIdentity kIdentity = {"mdb", "mdb> ", "mdb", false};

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "firmware %s", mdbcore::kFirmwareVersion);
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    settings_init(kIdentity);
    static Settings settings;
    settings_load(settings);
    console_start(kIdentity, settings);
    start_comms(kIdentity, settings);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
