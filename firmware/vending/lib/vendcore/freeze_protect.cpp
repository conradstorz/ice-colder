#include "freeze_protect.h"

namespace vendcore {

void FreezeProtect::tick(const InputSnapshot& raw, OutputGuard& out, uint32_t now_ms) {
    constexpr uint32_t kHeaterMaxOnMs = 86400000;  // 24 h: the heater legitimately runs for hours
    if (!raw.ambient_valid) {
        on_ = false;
    } else if (raw.ambient_c < on_below_c_) {
        on_ = true;
    } else if (raw.ambient_c > off_above_c_) {
        on_ = false;
    }
    out.request(Out::Heater, on_, kHeaterMaxOnMs, now_ms);
}

}  // namespace vendcore
