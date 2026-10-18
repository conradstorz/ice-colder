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
    void tick(const InputSnapshot& raw, OutputGuard& out, uint32_t now_ms);

private:
    float on_below_c_;
    float off_above_c_;
    bool on_ = false;
};

}  // namespace vendcore
