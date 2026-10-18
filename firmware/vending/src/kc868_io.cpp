#include "kc868_io.h"

#include "channels.h"
#include "driver/gpio.h"
#include "ds18b20.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "../boards/kc868-a8.h"
#include "onewire_bus.h"

namespace {
const char* TAG = "kc868_io";
constexpr int kI2cTimeoutMs = 5;
constexpr int kPcntHigh = 32767;
constexpr uint32_t kOneWirePeriodMs = 10000;
}  // namespace

esp_err_t Kc868Io::begin() {
    i2c_master_bus_config_t bc = {};
    bc.i2c_port = I2C_NUM_0;
    bc.sda_io_num = static_cast<gpio_num_t>(kc868::kGpioSda);
    bc.scl_io_num = static_cast<gpio_num_t>(kc868::kGpioScl);
    bc.clk_source = I2C_CLK_SRC_DEFAULT;
    bc.glitch_ignore_cnt = 7;
    bc.flags.enable_internal_pullup = true;
    esp_err_t err = i2c_new_master_bus(&bc, &bus_);
    if (err != ESP_OK) return err;
    i2c_device_config_t dc = {};
    dc.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    dc.scl_speed_hz = 100000;
    dc.device_address = kc868::kAddrRelays;
    err = i2c_master_bus_add_device(bus_, &dc, &relays_);
    if (err != ESP_OK) return err;
    dc.device_address = kc868::kAddrInputs;
    err = i2c_master_bus_add_device(bus_, &dc, &inputs_);
    if (err != ESP_OK) return err;

    // Inputs: write 0xFF once so the PCF8574 pins are released (quasi-bidirectional inputs).
    const uint8_t release = 0xFF;
    i2c_master_transmit(inputs_, &release, 1, kI2cTimeoutMs);
    const bool none[vendcore::kOutputCount] = {};
    write_outputs(none);  // every relay off before anything else runs

    gpio_config_t g = {};
    g.pin_bit_mask = 1ULL << kc868::kGpioServiceDoor;
    g.mode = GPIO_MODE_INPUT;
    g.pull_up_en = GPIO_PULLUP_ENABLE;
    gpio_config(&g);
    g = {};
    g.pin_bit_mask = 1ULL << kc868::kGpioWatchdogKick;
    g.mode = GPIO_MODE_OUTPUT;
    gpio_config(&g);
    gpio_set_level(static_cast<gpio_num_t>(kc868::kGpioWatchdogKick), 0);

    err = init_pulse_counter();
    if (err != ESP_OK) return err;
    err = init_adc();
    if (err != ESP_OK) return err;
    xTaskCreate(&Kc868Io::onewire_task, "onewire", 4096, this, 2, nullptr);
    return ESP_OK;
}

esp_err_t Kc868Io::init_pulse_counter() {
    pcnt_unit_config_t uc = {};
    uc.high_limit = kPcntHigh;
    uc.low_limit = -1;
    uc.flags.accum_count = true;  // driver accumulates across overflow: read gives a 32-bit total
    esp_err_t err = pcnt_new_unit(&uc, &pcnt_);
    if (err != ESP_OK) return err;
    pcnt_glitch_filter_config_t fc = {};
    fc.max_glitch_ns = 1000;
    err = pcnt_unit_set_glitch_filter(pcnt_, &fc);
    if (err != ESP_OK) return err;
    pcnt_chan_config_t cc = {};
    cc.edge_gpio_num = kc868::kGpioFlowPulse;
    cc.level_gpio_num = -1;
    pcnt_channel_handle_t ch = nullptr;
    err = pcnt_new_channel(pcnt_, &cc, &ch);
    if (err != ESP_OK) return err;
    err = pcnt_channel_set_edge_action(ch, PCNT_CHANNEL_EDGE_ACTION_INCREASE, PCNT_CHANNEL_EDGE_ACTION_HOLD);
    if (err != ESP_OK) return err;
    err = pcnt_unit_enable(pcnt_);
    if (err != ESP_OK) return err;
    err = pcnt_unit_clear_count(pcnt_);
    if (err != ESP_OK) return err;
    return pcnt_unit_start(pcnt_);
}

esp_err_t Kc868Io::init_adc() {
    adc_oneshot_unit_init_cfg_t uc = {};
    uc.unit_id = ADC_UNIT_1;
    esp_err_t err = adc_oneshot_new_unit(&uc, &adc_);
    if (err != ESP_OK) return err;
    adc_oneshot_chan_cfg_t cc = {};
    cc.atten = ADC_ATTEN_DB_12;
    cc.bitwidth = ADC_BITWIDTH_12;
    return adc_oneshot_config_channel(adc_, ADC_CHANNEL_6, &cc);  // GPIO 34
}

bool Kc868Io::read(vendcore::InputSnapshot& out) {
    uint8_t bits = 0;
    if (i2c_master_receive(inputs_, &bits, 1, kI2cTimeoutMs) != ESP_OK) return false;
    for (int i = 0; i < vendcore::kBinaryInputCount; ++i) {
        const uint8_t b = kc868::kInputBit[i];
        out.binary[i] = b == 0xFF ? gpio_get_level(static_cast<gpio_num_t>(kc868::kGpioServiceDoor)) != 0
                                  : ((bits >> b) & 1) != 0;
    }
    int count = 0;
    pcnt_unit_get_count(pcnt_, &count);
    out.pulses = static_cast<uint32_t>(count);
    out.ambient_c = ambient_c_;
    out.ambient_valid = ambient_valid_;
    int raw = 0;
    // One oneshot read is tens of microseconds, so it runs every tick.
    out.master_enable = adc_oneshot_read(adc_, ADC_CHANNEL_6, &raw) == ESP_OK &&
                        raw >= kc868::kMasterEnableRawThreshold;
    return true;
}

bool Kc868Io::write_outputs(const bool (&on)[vendcore::kOutputCount]) {
    uint8_t byte = kc868::kRelayActiveLow ? 0xFF : 0x00;
    for (int i = 0; i < vendcore::kOutputCount; ++i) {
        if (!on[i]) continue;
        const uint8_t mask = static_cast<uint8_t>(1U << kc868::kRelayBit[i]);
        byte = kc868::kRelayActiveLow ? static_cast<uint8_t>(byte & ~mask) : static_cast<uint8_t>(byte | mask);
    }
    if (byte == last_relay_byte_) return true;
    if (i2c_master_transmit(relays_, &byte, 1, kI2cTimeoutMs) != ESP_OK) {
        last_relay_byte_ = -1;  // retry on the next call
        return false;
    }
    last_relay_byte_ = byte;
    return true;
}

void Kc868Io::kick_watchdog() {
    const gpio_num_t pin = static_cast<gpio_num_t>(kc868::kGpioWatchdogKick);
    gpio_set_level(pin, 1);
    esp_rom_delay_us(50);
    gpio_set_level(pin, 0);
}

// Low-priority helper: the only code that touches the 1-Wire bus.
void Kc868Io::onewire_task(void* arg) {
    Kc868Io* self = static_cast<Kc868Io*>(arg);
    onewire_bus_handle_t bus = nullptr;
    onewire_bus_config_t bc = {};
    bc.bus_gpio_num = kc868::kGpioOneWire;
    onewire_bus_rmt_config_t rc = {};
    rc.max_rx_bytes = 10;
    ds18b20_device_handle_t sensor = nullptr;
    if (onewire_new_bus_rmt(&bc, &rc, &bus) != ESP_OK) {
        ESP_LOGE(TAG, "1-wire bus init failed");
        vTaskDelete(nullptr);
    }
    for (;;) {
        if (sensor == nullptr) {
            onewire_device_iter_handle_t iter = nullptr;
            onewire_device_t dev;
            if (onewire_new_device_iter(bus, &iter) == ESP_OK) {
                if (onewire_device_iter_get_next(iter, &dev) == ESP_OK) {
                    ds18b20_config_t cfg = {};
                    if (ds18b20_new_device(&dev, &cfg, &sensor) != ESP_OK) sensor = nullptr;
                }
                onewire_del_device_iter(iter);
            }
        }
        if (sensor != nullptr) {
            float t = 0.0f;
            if (ds18b20_trigger_temperature_conversion(sensor) == ESP_OK) {
                vTaskDelay(pdMS_TO_TICKS(800));
                if (ds18b20_get_temperature(sensor, &t) == ESP_OK) {
                    self->ambient_c_ = t;
                    self->ambient_valid_ = true;
                } else {
                    self->ambient_valid_ = false;
                }
            } else {
                self->ambient_valid_ = false;
            }
        } else {
            self->ambient_valid_ = false;
        }
        vTaskDelay(pdMS_TO_TICKS(kOneWirePeriodMs));
    }
}
