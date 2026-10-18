#pragma once

#include <stdint.h>

#include "driver/i2c_master.h"
#include "driver/pulse_cnt.h"
#include "esp_adc/adc_oneshot.h"
#include "ports.h"

// The KC868-A8 hardware port: two PCF8574 expanders, a flow-meter pulse counter, the
// service-door GPIO, the master-enable ADC readback and the external watchdog kick.
// It makes no decisions. The ambient temperature is the last value the 1-Wire helper
// stored; read() never touches the 1-Wire bus.
class Kc868Io : public vendcore::IoPort {
public:
    // Brings up every peripheral and the 1-Wire helper task. Call once, from app context.
    esp_err_t begin();

    bool read(vendcore::InputSnapshot& out) override;
    bool write_outputs(const bool (&on)[vendcore::kOutputCount]) override;
    void kick_watchdog() override;

private:
    esp_err_t init_pulse_counter();
    esp_err_t init_adc();
    static void onewire_task(void* arg);

    i2c_master_bus_handle_t bus_ = nullptr;
    i2c_master_dev_handle_t relays_ = nullptr;
    i2c_master_dev_handle_t inputs_ = nullptr;
    pcnt_unit_handle_t pcnt_ = nullptr;
    adc_oneshot_unit_handle_t adc_ = nullptr;
    int last_relay_byte_ = -1;  // -1 forces the next write
    volatile float ambient_c_ = 0.0f;
    volatile bool ambient_valid_ = false;
};
