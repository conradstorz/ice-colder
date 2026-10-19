// Per-device state mirrored into payment/state.
#pragma once

#include <stdint.h>

#include "events.h"

namespace mdbcore {

struct TubeLevel {
    uint32_t cents;  // value of ONE coin of this type
    uint16_t count;
    bool full;
};

struct DeviceState {
    bool present;
    Status status;
    int8_t level;  // 0 = null
    char model[13];
    char serial[13];
    uint32_t denominations[16];
    uint8_t n_denominations;
    uint32_t enabled[16];
    uint8_t n_enabled;
};

}  // namespace mdbcore
