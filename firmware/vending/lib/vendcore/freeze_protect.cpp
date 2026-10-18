#include "freeze_protect.h"

namespace vendcore {

void FreezeProtect::tick(const InputSnapshot& raw, OutputGuard& out, uint32_t now_ms) {
    if (!raw.ambient_valid) {
        on_ = false;
    } else if (raw.ambient_c < on_below_c_) {
        on_ = true;
    } else if (raw.ambient_c > off_above_c_) {
        on_ = false;
    }
    if (resting_) {
        if (now_ms - rest_since_ < kRestMs) {
            out.request(Out::Heater, false, kGuardBoundMs, now_ms);
            return;
        }
        resting_ = false;
    }
    if (on_) {
        if (!running_) {
            running_ = true;
            run_since_ = now_ms;
        } else if (now_ms - run_since_ >= kMaxContinuousOnMs) {
            resting_ = true;
            rest_since_ = now_ms;
            running_ = false;
            out.request(Out::Heater, false, kGuardBoundMs, now_ms);
            return;
        }
    } else {
        running_ = false;
    }
    out.request(Out::Heater, on_, kGuardBoundMs, now_ms);
}

void FreezeProtect::hold_off(OutputGuard& out, uint32_t now_ms) {
    running_ = false;
    out.request(Out::Heater, false, kGuardBoundMs, now_ms);
}

}  // namespace vendcore
