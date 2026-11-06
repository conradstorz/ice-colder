#pragma once

#include <stdint.h>

#include "boardidf/clock.h"
#include "ports.h"

struct EspClock : mdbcore::Clock {
    uint32_t now_ms() const override { return boardidf::mono_ms(); }
    uint64_t now_us() const override { return boardidf::mono_us(); }
};
