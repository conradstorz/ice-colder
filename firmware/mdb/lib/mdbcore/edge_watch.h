// Detects silence from the edge, measured from the last cmd/mdb message only.
#pragma once

#include <stdint.h>

#include "ports.h"

namespace mdbcore {

class EdgeWatch {
public:
    // Boot counts as the start of the silence window.
    explicit EdgeWatch(Clock& clock) : clock_(clock), last_ms_(clock.now_ms()) {}

    void seen() {
        last_ms_ = clock_.now_ms();
        reported_ = false;
    }
    void set_timeout_ms(uint32_t ms) { timeout_ms_ = ms; }
    bool expired() const { return timeout_ms_ > 0 && clock_.now_ms() - last_ms_ >= timeout_ms_; }
    // True once per silence onset; re-arms on the next seen().
    bool take_silence_edge() {
        if (!expired() || reported_) return false;
        reported_ = true;
        return true;
    }

private:
    Clock& clock_;
    uint32_t last_ms_;
    uint32_t timeout_ms_ = 30000;
    bool reported_ = false;
};

}  // namespace mdbcore
