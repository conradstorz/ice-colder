#include "settings.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../boards/kc868-a8.h"
#include "nvs.h"

#ifndef VEND_DEFAULT_WIFI_SSID
#define VEND_DEFAULT_WIFI_SSID ""
#endif
#ifndef VEND_DEFAULT_WIFI_PASS
#define VEND_DEFAULT_WIFI_PASS ""
#endif
#ifndef VEND_DEFAULT_MQTT_HOST
#define VEND_DEFAULT_MQTT_HOST ""
#endif
#ifndef VEND_DEFAULT_MQTT_PORT
#define VEND_DEFAULT_MQTT_PORT 1883
#endif
#ifndef VEND_DEFAULT_MQTT_USER
#define VEND_DEFAULT_MQTT_USER ""
#endif
#ifndef VEND_DEFAULT_MQTT_PASS
#define VEND_DEFAULT_MQTT_PASS ""
#endif
#ifndef VEND_DEFAULT_MACHINE_ID
#define VEND_DEFAULT_MACHINE_ID ""
#endif

namespace {
constexpr const char* kNs = "vend";

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

void settings_defaults(Settings& s) {
    memset(&s, 0, sizeof(s));
    copy(s.wifi_ssid, sizeof(s.wifi_ssid), VEND_DEFAULT_WIFI_SSID);
    copy(s.wifi_pass, sizeof(s.wifi_pass), VEND_DEFAULT_WIFI_PASS);
    copy(s.mqtt_host, sizeof(s.mqtt_host), VEND_DEFAULT_MQTT_HOST);
    copy(s.mqtt_user, sizeof(s.mqtt_user), VEND_DEFAULT_MQTT_USER);
    copy(s.mqtt_pass, sizeof(s.mqtt_pass), VEND_DEFAULT_MQTT_PASS);
    copy(s.machine_id, sizeof(s.machine_id), VEND_DEFAULT_MACHINE_ID);
    s.mqtt_port = VEND_DEFAULT_MQTT_PORT;
    const vendcore::Polarity p = kc868::default_polarity();
    for (int i = 0; i < vendcore::kBinaryInputCount; ++i) s.polarity[i] = p.active_low[i] ? 1 : 0;
}

esp_err_t settings_load(Settings& s) {
    settings_defaults(s);
    nvs_handle_t h;
    const esp_err_t err = nvs_open(kNs, NVS_READONLY, &h);
    if (err != ESP_OK) return err == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : err;  // nothing stored yet
    for (const StrKey& k : kStrKeys) {
        size_t len = k.size;
        nvs_get_str(h, k.key, field(s, k), &len);
    }
    nvs_get_u16(h, "mqtt_port", &s.mqtt_port);
    uint8_t eth = 0;
    if (nvs_get_u8(h, "use_eth", &eth) == ESP_OK) s.use_eth = eth != 0;
    uint8_t blob[vendcore::kBinaryInputCount];
    size_t blen = sizeof(blob);
    if (nvs_get_blob(h, "polarity", blob, &blen) == ESP_OK && blen == sizeof(blob)) {
        memcpy(s.polarity, blob, sizeof(blob));
    }
    nvs_close(h);
    return ESP_OK;
}

bool settings_set(const char* key, const char* value) {
    nvs_handle_t h;
    if (nvs_open(kNs, NVS_READWRITE, &h) != ESP_OK) return false;
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
    } else if (strcmp(key, "polarity") == 0 && strlen(value) == static_cast<size_t>(vendcore::kBinaryInputCount)) {
        uint8_t blob[vendcore::kBinaryInputCount];
        bool ok = true;
        for (int i = 0; i < vendcore::kBinaryInputCount; ++i) {
            ok = ok && (value[i] == '0' || value[i] == '1');
            blob[i] = value[i] == '1';
        }
        if (ok) err = nvs_set_blob(h, key, blob, sizeof(blob));
    }
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK;
}

void settings_erase() {
    nvs_handle_t h;
    if (nvs_open(kNs, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_erase_all(h);
    nvs_commit(h);
    nvs_close(h);
}

void settings_show(const Settings& s) {
    for (const StrKey& k : kStrKeys) {
        const char* v = reinterpret_cast<const char*>(&s) + k.offset;
        printf("%s = %s\n", k.key, k.secret ? (v[0] ? "********" : "") : v);
    }
    printf("mqtt_port = %u\nuse_eth = %d\npolarity = ", static_cast<unsigned>(s.mqtt_port), s.use_eth ? 1 : 0);
    for (int i = 0; i < vendcore::kBinaryInputCount; ++i) putchar(s.polarity[i] ? '1' : '0');
    printf("  (1 = active_low, in input channel order)\n");
}
