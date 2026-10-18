// Debounced, polarity-applied binary input scanner.
#pragma once

#include <stdint.h>

#include "channels.h"
#include "ports.h"

namespace vendcore {

struct Polarity {
    bool active_low[kBinaryInputCount];  // true: raw 0 means asserted
};

class InputScanner {
public:
    InputScanner(const Polarity& p, uint32_t debounce_ms = 30);

    // Tick with the raw snapshot; emits InputChanged / ButtonPressed on stable edges.
    void scan(const InputSnapshot& raw, uint32_t now_ms, Publisher& pub);
    bool state(In which) const;  // debounced, polarity-applied
    void republish_all(Publisher& pub) const;
    uint32_t pulses() const { return pulses_; }

private:
    Polarity pol_;
    uint32_t debounce_ms_;
    bool primed_ = false;
    bool stable_[kBinaryInputCount] = {};
    bool candidate_[kBinaryInputCount] = {};
    uint32_t since_[kBinaryInputCount] = {};
    uint32_t pulses_ = 0;
};

}  // namespace vendcore
