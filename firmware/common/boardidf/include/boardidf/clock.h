#pragma once

#include <stdint.h>

#include "esp_timer.h"

namespace boardidf {

// Monotonic time since boot. mono_ms wraps at 49 days; the cores use unsigned deltas.
inline uint32_t mono_ms() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }
inline uint64_t mono_us() { return static_cast<uint64_t>(esp_timer_get_time()); }

}  // namespace boardidf
