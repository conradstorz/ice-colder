// KC868-A8 (ESP32) pin map for the vending board. Every entry below is a working
// assumption: verify against KC868-A8-schematic.pdf before wiring the machine.
// Tags (IC5, W1, ...) are the IM600XL I/O list tags, docs/hardware/im600xl-io-list.md.
#pragma once

#include <stdint.h>

#include "channels.h"
#include "inputs.h"

namespace kc868 {

// I2C bus to the two PCF8574 expanders (100 kHz).
constexpr int kGpioSda = 4;   // verify against KC868-A8-schematic.pdf
constexpr int kGpioScl = 5;   // verify against KC868-A8-schematic.pdf
constexpr uint8_t kAddrRelays = 0x24;  // relay expander; verify against KC868-A8-schematic.pdf
constexpr uint8_t kAddrInputs = 0x22;  // opto-input expander; verify against KC868-A8-schematic.pdf
// KC868-A8 relays are driven through an inverting stage: a low pin energizes the coil.
constexpr bool kRelayActiveLow = true;  // verify against KC868-A8-schematic.pdf

constexpr int kGpioServiceDoor = 13;  // C6 service door switch, pull-up; verify against KC868-A8-schematic.pdf
constexpr int kGpioOneWire = 14;      // E2 ambient DS18B20; verify against KC868-A8-schematic.pdf
constexpr int kGpioFlowPulse = 32;    // W4 flow meter pulses; verify against KC868-A8-schematic.pdf
constexpr int kGpioWatchdogKick = 33; // external watchdog kick, 50 us pulse; verify against KC868-A8-schematic.pdf
constexpr int kGpioMasterEnable = 34; // master-enable readback; ADC1 channel 6
constexpr float kMasterEnableThresholdV = 1.5f;
// Raw 12-bit count for 1.5 V at 12 dB attenuation (about 3.1 V full scale, uncalibrated).
constexpr int kMasterEnableRawThreshold = 1980;

// Relay bit per output (index = vendcore::Out ordinal).
constexpr uint8_t kRelayBit[vendcore::kOutputCount] = {
    0,  // Agitator       IC1 bin agitator motor
    1,  // Auger          IC2 dispense auger motor
    2,  // BagFan         IC3 bag fan
    3,  // TrapDoorCatch  IC4 trap-door magnetic catch
    4,  // WaterValve     W3  dispense valve
    5,  // RoProduction   W5  RO production
    6,  // Heater         E1  heater power
};  // every bit: verify against KC868-A8-schematic.pdf

// Input expander bit per binary input (index = vendcore::In ordinal); In::ServiceDoor
// is read from kGpioServiceDoor instead, so its entry is unused (0xFF).
constexpr uint8_t kInputBit[vendcore::kBinaryInputCount] = {
    0,     // BagFull          IC5
    1,     // ButtonIce        C1
    2,     // ButtonWater1     W1
    3,     // ButtonWater5     W2
    4,     // RoTankFull       W6
    5,     // RoTankEmpty      W7
    6,     // WaterPressureOk  W8
    7,     // SumpHigh         W9
    0xFF,  // ServiceDoor      C6 (GPIO)
};  // every bit: verify against KC868-A8-schematic.pdf

// Default polarity: active_low true = raw 0 means asserted. Buttons (W1, W2, C1, normally
// open) and IC5 are active-high; W6-W9 and C6 default active-low so an open circuit reads
// as faulted (survey pending, I/O list section 8).
inline vendcore::Polarity default_polarity() {
    vendcore::Polarity p = {};
    p.active_low[static_cast<int>(vendcore::In::BagFull)] = false;          // IC5
    p.active_low[static_cast<int>(vendcore::In::ButtonIce)] = false;        // C1
    p.active_low[static_cast<int>(vendcore::In::ButtonWater1)] = false;     // W1
    p.active_low[static_cast<int>(vendcore::In::ButtonWater5)] = false;     // W2
    p.active_low[static_cast<int>(vendcore::In::RoTankFull)] = true;        // W6
    p.active_low[static_cast<int>(vendcore::In::RoTankEmpty)] = true;       // W7
    p.active_low[static_cast<int>(vendcore::In::WaterPressureOk)] = true;   // W8
    p.active_low[static_cast<int>(vendcore::In::SumpHigh)] = true;          // W9
    p.active_low[static_cast<int>(vendcore::In::ServiceDoor)] = true;       // C6
    return p;
}

}  // namespace kc868
