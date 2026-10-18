// RO fill loop: runs the RO pump while the tank is not full, latching off after an overrun.
#pragma once

#include <stdint.h>

#include "inputs.h"
#include "interlocks.h"
#include "ports.h"

namespace vendcore {

class RoLoop {
public:
    explicit RoLoop(uint32_t max_run_ms = 5400000) : max_run_ms_(max_run_ms) {}
    void tick(const InputScanner& in, OutputGuard& out, uint32_t now_ms, Publisher& pub);
    bool latched() const { return latched_; }

private:
    uint32_t max_run_ms_;
    bool running_ = false;
    bool latched_ = false;
    uint32_t started_ms_ = 0;
};

}  // namespace vendcore
