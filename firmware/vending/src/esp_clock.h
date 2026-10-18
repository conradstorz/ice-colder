#pragma once

#include <stdint.h>

#include "esp_timer.h"
#include "ports.h"

// Monotonic milliseconds since boot (wraps at 49 days; the core uses unsigned deltas).
struct EspClock : vendcore::Clock {
    uint32_t now_ms() const override { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }
};
