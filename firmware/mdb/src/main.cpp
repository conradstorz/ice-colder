#include "../boards/mdb-s3.h"
#include "boardidf/board_identity.h"
#include "boardidf/console.h"
#include "boardidf/settings.h"
#include "bus_task.h"
#include "comms_task.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "flash_window.h"
#include "ledger_task.h"
#include "nvs_flash.h"
#include "queues.h"
#include "status_led.h"
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
    flash_window_init();  // before the console: its settings writes go through the window
    settings_set_flash_guard(flash_window_open_blocking, flash_window_close_blocking);
    console_start(kIdentity, settings);

    // All queues are allocated here, once; nothing allocates after boot.
    static Queues q;
    q.cmd_q = xQueueCreate(8, sizeof(CmdItem));
    q.evt_q = xQueueCreate(64, sizeof(mdbcore::PaymentEvent));
    q.pub_q = xQueueCreate(32, sizeof(PubItem));
    q.ack_q = xQueueCreate(16, sizeof(boardcore::Ack));
    q.ack_in_q = xQueueCreate(8, sizeof(EventAckItem));
    q.state_q = xQueueCreate(1, sizeof(StateMsg));
    if (!q.cmd_q || !q.evt_q || !q.pub_q || !q.ack_q || !q.ack_in_q || !q.state_q) {
        ESP_LOGE(TAG, "queue allocation failed; halting");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    status_led_start(mdbboard::kStatusLedGpio);
    if (!ledger_init(q, settings.machine_id)) ESP_LOGE(TAG, "replay log unavailable; money acceptance stops (log pressure) until it is repaired");
    // Comms first: network init and PHY calibration touch flash, and must finish before the bus
    // task starts polling. The bus task is created only after start_comms has returned.
    start_comms(q, kIdentity, settings);
    start_ledger();
    start_bus(q);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
