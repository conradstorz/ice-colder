#include "comms_task.h"

#include <stdio.h>

#include "boardidf/mqtt_publisher.h"
#include "boardidf/net.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "version.h"
#include "wire.h"

using namespace mdbcore;

namespace {
const char* TAG = "comms";
constexpr uint32_t kHeartbeatMs = 10000;

struct Comms {
    Settings settings;
    bool caps_pending = false;  // retained capabilities publish not yet accepted
};
Comms g;

uint32_t uptime_s() { return static_cast<uint32_t>(esp_timer_get_time() / 1000000); }

WireCtx make_ctx(char* iso, size_t iso_cap) {
    WireCtx c;
    c.machine_id = g.settings.machine_id;
    c.hardware_id = net_hardware_id();
    c.ip = net_ip();
    c.firmware = kFirmwareVersion;
    c.iso_time = net_iso_time(iso, iso_cap) ? iso : nullptr;
    return c;
}

void publish_heartbeat() {
    static char buf[96];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    char tail[48];
    snprintf(tail, sizeof(tail), "heartbeat/%s", kSubsystem);
    const size_t n = heartbeat_json(uptime_s(), buf, sizeof(buf));
    if (n > 0 && topic_join(ctx, tail, topic, sizeof(topic)) > 0) mqtt_publish(topic, buf, n, false);
}

void publish_capabilities() {
    static char payload[1024];
    static char topic[160];
    char iso[24];
    const WireCtx ctx = make_ctx(iso, sizeof(iso));
    char tail[48];
    snprintf(tail, sizeof(tail), "capabilities/%s", kSubsystem);
    const size_t n = capabilities_json(ctx, payload, sizeof(payload));
    if (n == 0 || topic_join(ctx, tail, topic, sizeof(topic)) == 0 || !mqtt_publish(topic, payload, n, true)) {
        ESP_LOGW(TAG, "capabilities publish failed; will retry");
        return;
    }
    g.caps_pending = false;
}

void comms_main(void*) {
    uint32_t last_hb = 0;
    for (;;) {
        if (mqtt_take_connected_edge()) {
            g.caps_pending = true;
            publish_capabilities();
            publish_heartbeat();
            last_hb = uptime_s();
        }
        if (mqtt_connected()) {
            if (g.caps_pending) publish_capabilities();
            if (uptime_s() - last_hb >= kHeartbeatMs / 1000) {
                last_hb = uptime_s();
                publish_heartbeat();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
}  // namespace

void start_comms(const BoardIdentity& id, const Settings& settings) {
    g.settings = settings;
    net_start(id, settings);
    mqtt_start(id, settings, [](const char*, size_t, const char*, size_t) {});
    xTaskCreatePinnedToCore(comms_main, "comms", 6144, nullptr, 5, nullptr, 0);
}
