// Hardware-facing seams; implemented by the board layer and by test fakes.
#pragma once

#include <stdint.h>

#include "channels.h"

namespace vendcore {

struct Clock {
    virtual uint32_t now_ms() const = 0;
};

struct InputSnapshot {
    bool binary[kBinaryInputCount];
    uint32_t pulses;
    float ambient_c;
    bool ambient_valid;
    bool master_enable;
};

struct IoPort {
    virtual bool read(InputSnapshot& out) = 0;  // false on I2C failure
    virtual bool write_outputs(const bool (&on)[kOutputCount]) = 0;
    virtual void kick_watchdog() = 0;
};

enum class EventType : uint8_t {
    InputChanged, ButtonPressed, Step, Outcome, Telemetry, Temperature, OutputChanged, Health
};

struct Event {
    EventType type;
    uint8_t channel;
    bool state;
    uint32_t value;
    float fvalue;
    char text[48];
    char request_id[65];
    uint8_t slot;
};

struct Publisher {
    virtual bool publish(const Event& e) = 0;  // false = queue full
};

}  // namespace vendcore
