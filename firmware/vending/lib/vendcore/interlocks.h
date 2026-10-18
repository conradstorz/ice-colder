// Output guard (the only writer of the output array), VMC liveness, interlocks and health gate.
#pragma once

#include <stdint.h>

#include "channels.h"
#include "inputs.h"
#include "ports.h"

namespace vendcore {

class OutputGuard {
public:
    // Sets the desired state of one output. Idempotent; the on-timer starts when desired goes off->on.
    void request(Out o, bool on, uint32_t max_on_ms, uint32_t now_ms);
    void all_off();
    // Forces overdue outputs off (clearing their desired state), writes the array, and emits one
    // OutputChanged per output whose written state changed since the last apply.
    void apply(uint32_t now_ms, bool (&out)[kOutputCount], Publisher& pub);
    bool any_motion() const;  // agitator, auger, catch, valve
    bool any_on() const;

private:
    bool want_[kOutputCount] = {};
    bool written_[kOutputCount] = {};
    bool forced_[kOutputCount] = {};  // forced off by max-on; ignores on-requests until requested off
    uint32_t on_since_[kOutputCount] = {};
    uint32_t max_on_[kOutputCount] = {};
};

class VmcLiveness {
public:
    explicit VmcLiveness(uint32_t silence_ms = 30000) : silence_ms_(silence_ms) {}
    void seen(uint32_t now_ms);
    void set_online(bool online, uint32_t now_ms);
    bool silent(uint32_t now_ms) const;

private:
    uint32_t silence_ms_;
    uint32_t last_seen_ = 0;
    bool online_ = false;  // boot state: VMC not yet heard
};

struct TripReason {
    const char* detail;  // "io_failure", "master_enable_dropped", "sump_high", "service_door", "vmc_offline"
};

class Interlocks {
public:
    // Reports the first rule that holds, every tick it holds.
    bool trip(const InputScanner& in, const InputSnapshot& raw, bool io_ok, const VmcLiveness& vmc,
              bool motion, bool any_on, uint32_t now_ms, TripReason& why) const;
};

class Health {
public:
    void set_io_ok(bool v) { io_ok_ = v; }
    void set_deadline_met(bool v) { deadline_met_ = v; }
    void set_latched(bool v) { latched_ = v; }
    bool ok() const { return io_ok_ && deadline_met_ && !latched_; }

private:
    bool io_ok_ = false;
    bool deadline_met_ = false;
    bool latched_ = false;
};

}  // namespace vendcore
