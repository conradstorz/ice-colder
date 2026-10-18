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
// No I/O-list tag: board-side signal (VMC-independent external watchdog), not an IM600XL point.
constexpr int kGpioWatchdogKick = 33; // external watchdog kick, 50 us pulse; verify against KC868-A8-schematic.pdf
constexpr int kAdcMasterEnable = 34;  // master-enable readback; ADC1 channel 6; verify against KC868-A8-schematic.pdf
constexpr float kMasterEnableThresholdV = 1.5f;  // verify against KC868-A8-schematic.pdf
// Raw 12-bit count for 1.5 V at 12 dB attenuation (about 3.1 V full scale, uncalibrated);
// verify against KC868-A8-schematic.pdf.
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

// Default polarity: active_low true = raw 0 means asserted. The KC868-A8 inputs are
// opto-isolated dry-contact inputs: a closed contact drives the pin low (raw 0); an open
// contact or broken wire reads raw 1 (pull-up). Every fault input must be wired
// normally-closed so a broken wire reads as the fault. Contact types are pending the
// I/O list section 8 survey (W6/W7/W8/W9/C6 contact types, IC5 sensor type); verify
// against KC868-A8-schematic.pdf.
inline vendcore::Polarity default_polarity() {
    vendcore::Polarity p = {};
    // IC5 bag-full sensor, closed = bag full; open = not full (fill runs to max_run_s -> bin_empty, visible fault).
    p.active_low[static_cast<int>(vendcore::In::BagFull)] = true;
    // C1 ice button, NO momentary; closed = pressed, open = idle.
    p.active_low[static_cast<int>(vendcore::In::ButtonIce)] = true;
    // W1 1 gal button, NO momentary; closed = pressed, open = idle.
    p.active_low[static_cast<int>(vendcore::In::ButtonWater1)] = true;
    // W2 5 gal button, NO momentary; closed = pressed, open = idle.
    p.active_low[static_cast<int>(vendcore::In::ButtonWater5)] = true;
    // W6 RO tank full, wired NC: closed while not full; open = full (pump stops, no overflow).
    p.active_low[static_cast<int>(vendcore::In::RoTankFull)] = false;
    // W7 RO tank empty, wired NC: closed while water present; open = empty.
    p.active_low[static_cast<int>(vendcore::In::RoTankEmpty)] = false;
    // W8 water pressure OK, closed = pressure OK; open = not OK.
    p.active_low[static_cast<int>(vendcore::In::WaterPressureOk)] = true;
    // W9 sump high float, wired NC: closed while normal; open = high water.
    p.active_low[static_cast<int>(vendcore::In::SumpHigh)] = false;
    // C6 service door switch, wired NC: closed while the door is closed; open = door open.
    p.active_low[static_cast<int>(vendcore::In::ServiceDoor)] = false;
    return p;
}

}  // namespace kc868
