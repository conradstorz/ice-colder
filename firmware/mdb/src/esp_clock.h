#pragma once

#include <stdint.h>

#include "esp_timer.h"
#include "ports.h"

// Monotonic time since boot. now_ms wraps at 49 days; the core uses unsigned deltas.
struct EspClock : mdbcore::Clock {
    uint32_t now_ms() const override { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }
    uint64_t now_us() const override { return static_cast<uint64_t>(esp_timer_get_time()); }
};
