#pragma once

#include <stdint.h>

#include "boardidf/board_identity.h"
#include "esp_err.h"

// Persistent configuration in the board's NVS namespace. Compile-time defaults come from the
// BOARD_DEFAULT_* -D flags (see each project's platformio.ini); all are empty when unset.
struct Settings {
    char wifi_ssid[33];
    char wifi_pass[65];
    char mqtt_host[65];
    uint16_t mqtt_port;
    char mqtt_user[33];
    char mqtt_pass[65];
    char machine_id[33];
    bool use_eth;
};

// A board-specific console setting; the board owns its storage and validation.
struct ExtraSetting {
    const char* key;
    const char* help;                // appended to the `set` command help
    bool (*set)(const char* value);  // validate and persist; false = rejected
    void (*show)();                  // print the "key = value" line(s) for `show`
};

// Registers the board's extra settings (the table must outlive the program).
void settings_register_extra(const ExtraSetting* table, int n);
int settings_extra_count();
const ExtraSetting* settings_extra(int index);

// Binds the NVS namespace; call once before any other settings function.
void settings_init(const BoardIdentity& id);
void settings_defaults(Settings& s);
esp_err_t settings_load(Settings& s);
// Returns false for an unknown key or a bad value.
bool settings_set(const char* key, const char* value);
void settings_erase();
// Prints every key (extras included); secrets are masked.
void settings_show(const Settings& s);
