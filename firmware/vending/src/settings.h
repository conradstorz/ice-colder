#pragma once

#include <stdint.h>

#include "esp_err.h"

// Persistent configuration in NVS namespace "vend". Compile-time defaults come from the
// VEND_DEFAULT_* -D flags (see platformio.ini); all are empty when unset.
struct Settings {
    char wifi_ssid[33];
    char wifi_pass[65];
    char mqtt_host[65];
    uint16_t mqtt_port;
    char mqtt_user[33];
    char mqtt_pass[65];
    char machine_id[33];
    bool use_eth;
    uint8_t polarity[9];  // per input channel, 1 = active_low; board default until set
};

void settings_defaults(Settings& s);
esp_err_t settings_load(Settings& s);
// Returns false for an unknown key or a bad value.
bool settings_set(const char* key, const char* value);
void settings_erase();
// Prints every key; secrets are masked.
void settings_show(const Settings& s);
