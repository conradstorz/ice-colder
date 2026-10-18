#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "version.h"

static const char* TAG = "vending";

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "firmware %s", vendcore::kFirmwareVersion);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
