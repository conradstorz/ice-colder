#include "ro_loop.h"

#include <string.h>

namespace vendcore {

void RoLoop::tick(const InputScanner& in, OutputGuard& out, uint32_t now_ms, Publisher& pub) {
    const uint32_t backstop_ms = max_run_ms_ + 1000;  // the guard trips only if this loop fails to
    if (in.state(In::RoTankFull)) {
        latched_ = false;
        running_ = false;
        out.request(Out::RoProduction, false, backstop_ms, now_ms);
        return;
    }
    if (latched_) {
        out.request(Out::RoProduction, false, backstop_ms, now_ms);
        return;
    }
    if (!running_) {
        running_ = true;
        started_ms_ = now_ms;
    }
    if (now_ms - started_ms_ > max_run_ms_) {
        latched_ = true;
        running_ = false;
        out.request(Out::RoProduction, false, backstop_ms, now_ms);
        Event e = {};
        e.type = EventType::Health;
        strncpy(e.text, "ro_overrun_latched", sizeof(e.text) - 1);
        pub.publish(e);
        return;
    }
    out.request(Out::RoProduction, true, backstop_ms, now_ms);
}

}  // namespace vendcore
