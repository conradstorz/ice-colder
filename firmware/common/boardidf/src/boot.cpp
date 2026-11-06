#include "boardidf/boot.h"

#include "boardidf/console.h"
#include "nvs_flash.h"

namespace boardidf {

void board_boot(const BoardIdentity& identity, Settings& settings, void (*after_load)()) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    settings_init(identity);
    settings_load(settings);
    if (after_load) after_load();
    console_start(identity, settings);
}

}  // namespace boardidf
