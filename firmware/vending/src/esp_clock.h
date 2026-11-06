#pragma once

#include <stdint.h>

#include "boardidf/clock.h"
#include "ports.h"

struct EspClock : vendcore::Clock {
    uint32_t now_ms() const override { return boardidf::mono_ms(); }
};
