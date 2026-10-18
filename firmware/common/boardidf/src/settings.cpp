#include "boardidf/settings.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nvs.h"

#ifndef BOARD_DEFAULT_WIFI_SSID
#define BOARD_DEFAULT_WIFI_SSID ""
#endif
#ifndef BOARD_DEFAULT_WIFI_PASS
#define BOARD_DEFAULT_WIFI_PASS ""
#endif
#ifndef BOARD_DEFAULT_MQTT_HOST
#define BOARD_DEFAULT_MQTT_HOST ""
#endif
#ifndef BOARD_DEFAULT_MQTT_PORT
#define BOARD_DEFAULT_MQTT_PORT 1883
#endif
#ifndef BOARD_DEFAULT_MQTT_USER
#define BOARD_DEFAULT_MQTT_USER ""
#endif
#ifndef BOARD_DEFAULT_MQTT_PASS
#define BOARD_DEFAULT_MQTT_PASS ""
#endif
#ifndef BOARD_DEFAULT_MACHINE_ID
#define BOARD_DEFAULT_MACHINE_ID ""
#endif

namespace {
const char* g_ns = "board";
const ExtraSetting* g_extra = nullptr;
int g_extra_n = 0;

struct StrKey {
    const char* key;
    size_t offset;
    size_t size;
    bool secret;
};
const StrKey kStrKeys[] = {
    {"wifi_ssid", offsetof(Settings, wifi_ssid), sizeof(Settings::wifi_ssid), false},
    {"wifi_pass", offsetof(Settings, wifi_pass), sizeof(Settings::wifi_pass), true},
    {"mqtt_host", offsetof(Settings, mqtt_host), sizeof(Settings::mqtt_host), false},
    {"mqtt_user", offsetof(Settings, mqtt_user), sizeof(Settings::mqtt_user), false},
    {"mqtt_pass", offsetof(Settings, mqtt_pass), sizeof(Settings::mqtt_pass), true},
    {"machine_id", offsetof(Settings, machine_id), sizeof(Settings::machine_id), false},
};

void copy(char* dst, size_t n, const char* src) {
    strncpy(dst, src, n - 1);
    dst[n - 1] = '\0';
}

char* field(Settings& s, const StrKey& k) { return reinterpret_cast<char*>(&s) + k.offset; }
}  // namespace

void settings_register_extra(const ExtraSetting* table, int n) {
    g_extra = table;
    g_extra_n = n;
}

int settings_extra_count() { return g_extra_n; }

const ExtraSetting* settings_extra(int index) {
    return (index >= 0 && index < g_extra_n) ? &g_extra[index] : nullptr;
}

void settings_init(const BoardIdentity& id) { g_ns = id.nvs_namespace; }

void settings_defaults(Settings& s) {
    memset(&s, 0, sizeof(s));
    copy(s.wifi_ssid, sizeof(s.wifi_ssid), BOARD_DEFAULT_WIFI_SSID);
    copy(s.wifi_pass, sizeof(s.wifi_pass), BOARD_DEFAULT_WIFI_PASS);
    copy(s.mqtt_host, sizeof(s.mqtt_host), BOARD_DEFAULT_MQTT_HOST);
    copy(s.mqtt_user, sizeof(s.mqtt_user), BOARD_DEFAULT_MQTT_USER);
    copy(s.mqtt_pass, sizeof(s.mqtt_pass), BOARD_DEFAULT_MQTT_PASS);
    copy(s.machine_id, sizeof(s.machine_id), BOARD_DEFAULT_MACHINE_ID);
    s.mqtt_port = BOARD_DEFAULT_MQTT_PORT;
}

esp_err_t settings_load(Settings& s) {
    settings_defaults(s);
    nvs_handle_t h;
    const esp_err_t err = nvs_open(g_ns, NVS_READONLY, &h);
    if (err != ESP_OK) return err == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : err;  // nothing stored yet
    for (const StrKey& k : kStrKeys) {
        size_t len = k.size;
        nvs_get_str(h, k.key, field(s, k), &len);
    }
    nvs_get_u16(h, "mqtt_port", &s.mqtt_port);
    uint8_t eth = 0;
    if (nvs_get_u8(h, "use_eth", &eth) == ESP_OK) s.use_eth = eth != 0;
    nvs_close(h);
    return ESP_OK;
}

bool settings_set(const char* key, const char* value) {
    for (int i = 0; i < g_extra_n; ++i) {
        if (strcmp(key, g_extra[i].key) == 0) return g_extra[i].set(value);
    }
    nvs_handle_t h;
    if (nvs_open(g_ns, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = ESP_ERR_INVALID_ARG;
    for (const StrKey& k : kStrKeys) {
        if (strcmp(key, k.key) == 0 && strlen(value) < k.size) err = nvs_set_str(h, key, value);
    }
    if (strcmp(key, "mqtt_port") == 0) {
        char* end = nullptr;
        const long v = strtol(value, &end, 10);
        if (end != value && *end == '\0' && v > 0 && v < 65536) {
            err = nvs_set_u16(h, key, static_cast<uint16_t>(v));
        }
    } else if (strcmp(key, "use_eth") == 0) {
        if (strcmp(value, "0") == 0 || strcmp(value, "1") == 0) err = nvs_set_u8(h, key, value[0] == '1');
    }
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK;
}

void settings_erase() {
    nvs_handle_t h;
    if (nvs_open(g_ns, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_erase_all(h);
    nvs_commit(h);
    nvs_close(h);
}

void settings_show(const Settings& s) {
    for (const StrKey& k : kStrKeys) {
        const char* v = reinterpret_cast<const char*>(&s) + k.offset;
        printf("%s = %s\n", k.key, k.secret ? (v[0] ? "********" : "") : v);
    }
    printf("mqtt_port = %u\nuse_eth = %d\n", static_cast<unsigned>(s.mqtt_port), s.use_eth ? 1 : 0);
    for (int i = 0; i < g_extra_n; ++i) g_extra[i].show();
}
