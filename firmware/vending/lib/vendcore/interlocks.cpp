#include "interlocks.h"

namespace vendcore {

void OutputGuard::request(Out o, bool on, uint32_t max_on_ms, uint32_t now_ms) {
    const int i = static_cast<int>(o);
    if (!on) forced_[i] = false;
    if (on && forced_[i]) return;
    if (on && !want_[i]) on_since_[i] = now_ms;
    want_[i] = on;
    max_on_[i] = max_on_ms;
}

void OutputGuard::all_off() {
    for (int i = 0; i < kOutputCount; ++i) {
        want_[i] = false;
    }
}

void OutputGuard::republish_all(Publisher& pub) const {
    for (int i = 0; i < kOutputCount; ++i) {
        Event e = {};
        e.type = EventType::OutputChanged;
        e.channel = static_cast<uint8_t>(kBinaryInputCount + i);
        e.state = written_[i];
        pub.publish(e);
    }
}

void OutputGuard::apply(uint32_t now_ms, bool (&out)[kOutputCount], Publisher& pub) {
    for (int i = 0; i < kOutputCount; ++i) {
        if (want_[i] && now_ms - on_since_[i] > max_on_[i]) {
            want_[i] = false;
            forced_[i] = true;
        }
        out[i] = want_[i];
        if (out[i] != written_[i]) {
            written_[i] = out[i];
            Event e = {};
            e.type = EventType::OutputChanged;
            e.channel = static_cast<uint8_t>(kBinaryInputCount + i);
            e.state = out[i];
            pub.publish(e);
        }
    }
}

bool OutputGuard::any_motion() const {
    return want_[static_cast<int>(Out::Agitator)] || want_[static_cast<int>(Out::Auger)] ||
           want_[static_cast<int>(Out::TrapDoorCatch)] || want_[static_cast<int>(Out::WaterValve)];
}

bool OutputGuard::any_on() const {
    for (int i = 0; i < kOutputCount; ++i)
        if (want_[i]) return true;
    return false;
}

void VmcLiveness::seen(uint32_t now_ms) {
    last_seen_ = now_ms;
    online_ = true;
}

void VmcLiveness::set_online(bool online, uint32_t now_ms) {
    if (online) {
        seen(now_ms);
    } else {
        online_ = false;
    }
}

bool VmcLiveness::silent(uint32_t now_ms) const { return !online_ || now_ms - last_seen_ > silence_ms_; }

bool Interlocks::trip(const InputScanner& in, const InputSnapshot& raw, bool io_ok, const VmcLiveness& vmc,
                      bool motion, bool any_on, uint32_t now_ms, TripReason& why) const {
    if (!io_ok) {
        why.detail = "io_failure";
    } else if (!raw.master_enable && any_on) {
        why.detail = "master_enable_dropped";
    } else if (in.state(In::SumpHigh)) {
        why.detail = "sump_high";
    } else if (in.state(In::ServiceDoor) && motion) {
        why.detail = "service_door";
    } else if (vmc.silent(now_ms)) {
        why.detail = "vmc_offline";
    } else {
        return false;
    }
    return true;
}

}  // namespace vendcore
