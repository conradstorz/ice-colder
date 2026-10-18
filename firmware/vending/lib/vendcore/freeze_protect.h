// Heater freeze protection with hysteresis on the ambient temperature.
#pragma once

#include <stdint.h>

#include "interlocks.h"
#include "ports.h"

namespace vendcore {

class FreezeProtect {
public:
    FreezeProtect(float on_below_c = 3.0f, float off_above_c = 6.0f)
        : on_below_c_(on_below_c), off_above_c_(off_above_c) {}
    // Hysteresis plus a duty cycle: after kMaxContinuousOnMs of continuous heating the heater is
    // requested off for kRestMs, then allowed on again. The guard's own max-on bound sits above
    // kMaxContinuousOnMs, so it never fires first (its forced latch only clears on request(off),
    // which a still-cold ambient would never produce).
    void tick(const InputSnapshot& raw, OutputGuard& out, uint32_t now_ms);
    // A latching interlock holds: heater off, and the continuous-on clock restarts.
    void hold_off(OutputGuard& out, uint32_t now_ms);

    static constexpr uint32_t kMaxContinuousOnMs = 86400000;  // 24 h
    static constexpr uint32_t kRestMs = 300000;               // 5 min
    static constexpr uint32_t kGuardBoundMs = kMaxContinuousOnMs + 60000;

private:
    float on_below_c_;
    float off_above_c_;
    bool on_ = false;
    bool running_ = false;  // heater requested on without a break since run_since_
    uint32_t run_since_ = 0;
    bool resting_ = false;
    uint32_t rest_since_ = 0;
};

}  // namespace vendcore
