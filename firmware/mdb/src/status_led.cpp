#include "status_led.h"

#include <atomic>

#include "boardidf/mqtt_publisher.h"
#include "driver/rmt_encoder.h"
#include "driver/rmt_tx.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {
const char* TAG = "led";
constexpr uint32_t kResolutionHz = 10000000;  // 0.1 us per tick
constexpr uint8_t kLevel = 24;                // dim: it sits next to a person's eyes

rmt_channel_handle_t g_chan = nullptr;
rmt_encoder_handle_t g_enc = nullptr;
std::atomic<bool> g_accepting{false};
std::atomic<bool> g_offline{false};
std::atomic<bool> g_log_full{false};

void show(uint8_t r, uint8_t g, uint8_t b) {
    const uint8_t grb[3] = {g, r, b};
    rmt_transmit_config_t tx = {};
    if (rmt_transmit(g_chan, g_enc, grb, sizeof(grb), &tx) == ESP_OK) rmt_tx_wait_all_done(g_chan, 20);
}

void led_main(void*) {
    for (;;) {
        const bool blink_on = ((xTaskGetTickCount() / pdMS_TO_TICKS(500)) & 1) == 0;  // 1 Hz blink
        if (g_log_full.load()) {
            show(blink_on ? kLevel : 0, blink_on ? kLevel : 0, blink_on ? kLevel : 0);
        } else if (!mqtt_connected()) {
            show(0, 0, blink_on ? kLevel : 0);
        } else if (g_offline.load()) {
            show(kLevel, 0, 0);
        } else if (g_accepting.load()) {
            show(0, kLevel, 0);
        } else {
            show(kLevel, kLevel / 2, 0);
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
}  // namespace

void status_led_start(int gpio) {
    rmt_tx_channel_config_t cc = {};
    cc.gpio_num = static_cast<gpio_num_t>(gpio);
    cc.clk_src = RMT_CLK_SRC_DEFAULT;
    cc.resolution_hz = kResolutionHz;
    cc.mem_block_symbols = 64;
    cc.trans_queue_depth = 2;
    rmt_bytes_encoder_config_t ec = {};
    ec.bit0.level0 = 1;
    ec.bit0.duration0 = 3;  // 0.3 us high
    ec.bit0.level1 = 0;
    ec.bit0.duration1 = 9;  // 0.9 us low
    ec.bit1.level0 = 1;
    ec.bit1.duration0 = 9;
    ec.bit1.level1 = 0;
    ec.bit1.duration1 = 3;
    ec.flags.msb_first = 1;
    if (rmt_new_tx_channel(&cc, &g_chan) != ESP_OK || rmt_new_bytes_encoder(&ec, &g_enc) != ESP_OK ||
        rmt_enable(g_chan) != ESP_OK) {
        ESP_LOGE(TAG, "status LED unavailable");
        return;
    }
    xTaskCreatePinnedToCore(led_main, "led", 3072, nullptr, 2, nullptr, 0);
}

void status_led_set_state(bool accepting, bool device_offline) {
    g_accepting.store(accepting);
    g_offline.store(device_offline);
}

void status_led_set_log_full(bool full) { g_log_full.store(full); }
