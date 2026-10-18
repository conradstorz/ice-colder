#include "inputs.h"

namespace vendcore {

namespace {
bool is_button(int i) {
    return i == static_cast<int>(In::ButtonIce) || i == static_cast<int>(In::ButtonWater1) ||
           i == static_cast<int>(In::ButtonWater5);
}
}  // namespace

InputScanner::InputScanner(const Polarity& p, uint32_t debounce_ms) : pol_(p), debounce_ms_(debounce_ms) {}

void InputScanner::scan(const InputSnapshot& raw, uint32_t now_ms, Publisher& pub) {
    pulses_ = raw.pulses;
    for (int i = 0; i < kBinaryInputCount; ++i) {
        const bool v = pol_.active_low[i] ? !raw.binary[i] : raw.binary[i];
        if (!primed_) {
            stable_[i] = v;
            candidate_[i] = v;
            since_[i] = now_ms;
            continue;
        }
        if (v == stable_[i]) {
            candidate_[i] = v;
            continue;
        }
        if (v != candidate_[i]) {
            candidate_[i] = v;
            since_[i] = now_ms;
        }
        if (now_ms - since_[i] >= debounce_ms_) {
            stable_[i] = v;
            Event e = {};
            e.type = EventType::InputChanged;
            e.channel = static_cast<uint8_t>(i);
            e.state = v;
            pub.publish(e);
            if (v && is_button(i)) {
                e.type = EventType::ButtonPressed;
                pub.publish(e);
            }
        }
    }
    primed_ = true;
}

bool InputScanner::state(In which) const { return stable_[static_cast<int>(which)]; }

void InputScanner::republish_all(Publisher& pub) const {
    for (int i = 0; i < kBinaryInputCount; ++i) {
        Event e = {};
        e.type = EventType::InputChanged;
        e.channel = static_cast<uint8_t>(i);
        e.state = stable_[i];
        pub.publish(e);
    }
}

}  // namespace vendcore
