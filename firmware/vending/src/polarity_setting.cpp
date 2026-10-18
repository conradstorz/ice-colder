#include "polarity_setting.h"

#include <stdio.h>
#include <string.h>

#include "../boards/kc868-a8.h"
#include "boardidf/settings.h"
#include "nvs.h"

namespace {
constexpr const char* kNs = "vend";
constexpr const char* kKey = "polarity";

bool polarity_set(const char* value) {
    if (strlen(value) != static_cast<size_t>(kPolarityCount)) return false;
    uint8_t blob[kPolarityCount];
    for (int i = 0; i < kPolarityCount; ++i) {
        if (value[i] != '0' && value[i] != '1') return false;
        blob[i] = value[i] == '1';
    }
    nvs_handle_t h;
    if (nvs_open(kNs, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(h, kKey, blob, sizeof(blob));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK;
}

void polarity_show() {
    uint8_t p[kPolarityCount];
    polarity_load(p);
    printf("polarity = ");
    for (int i = 0; i < kPolarityCount; ++i) putchar(p[i] ? '1' : '0');
    printf("  (1 = active_low, in input channel order)\n");
}

const ExtraSetting kExtras[] = {
    {kKey, "polarity: 9 chars of 0/1, 1 = active_low", polarity_set, polarity_show},
};
}  // namespace

void polarity_register() { settings_register_extra(kExtras, 1); }

void polarity_load(uint8_t out[kPolarityCount]) {
    const vendcore::Polarity p = kc868::default_polarity();
    for (int i = 0; i < kPolarityCount; ++i) out[i] = p.active_low[i] ? 1 : 0;
    nvs_handle_t h;
    if (nvs_open(kNs, NVS_READONLY, &h) != ESP_OK) return;
    uint8_t blob[kPolarityCount];
    size_t blen = sizeof(blob);
    if (nvs_get_blob(h, kKey, blob, &blen) == ESP_OK && blen == sizeof(blob)) memcpy(out, blob, sizeof(blob));
    nvs_close(h);
}
