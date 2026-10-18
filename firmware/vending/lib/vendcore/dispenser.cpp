#include "dispenser.h"

#include <stdio.h>
#include <string.h>

namespace vendcore {

namespace {
constexpr uint8_t kBitAgitate = 1, kBitFill = 2, kBitRelease = 4;

uint32_t to_ms(float s) { return s <= 0.0f ? 0u : static_cast<uint32_t>(s * 1000.0f + 0.5f); }
bool reached(uint32_t now, uint32_t t) { return static_cast<int32_t>(now - t) >= 0; }
void copy_text(char* dst, size_t n, const char* src) {
    strncpy(dst, src ? src : "", n - 1);
    dst[n - 1] = '\0';
}
}  // namespace

const char* step_name(Step s) {
    switch (s) {
        case Step::Agitate: return "agitate";
        case Step::Fill: return "fill";
        case Step::Release: return "release";
    }
    return "";
}

const char* outcome_name(Outcome o) {
    switch (o) {
        case Outcome::Complete: return "complete";
        case Outcome::BinEmpty: return "bin_empty";
        case Outcome::Timeout: return "timeout";
        case Outcome::Jam: return "jam";
        case Outcome::Error: return "error";
        case Outcome::DoorOpen: return "door_open";
        case Outcome::NoFlow: return "no_flow";
        case Outcome::OverDispense: return "over_dispense";
    }
    return "";
}

void Dispenser::set_out(int channel, bool on) {
    if (!is_output(channel)) return;
    on_[static_cast<int>(out_from_index(channel))] = on;
}

void Dispenser::begin_step(uint8_t bit) {
    for (int i = 0; i < p_.acc_count; ++i) {
        if ((p_.acc[i].on_during_mask & bit) && acc_state_[i] == AccState::Pending) {
            acc_state_[i] = AccState::On;
            set_out(p_.acc[i].channel, true);
        }
    }
}

void Dispenser::end_step(uint8_t bit, uint32_t now) {
    for (int i = 0; i < p_.acc_count; ++i) {
        uint8_t m = p_.acc[i].on_during_mask;
        // Last step in the mask: no mask bit above this one.
        if ((m & bit) && (m & ~static_cast<uint8_t>((bit << 1) - 1)) == 0 && acc_state_[i] == AccState::On) {
            acc_armed_[i] = true;
            acc_off_at_[i] = now + acc_lag_ms_[i];
        }
    }
}

void Dispenser::emit_step(Step s, Publisher& pub) {
    Event e = {};
    e.type = EventType::Step;
    copy_text(e.text, sizeof(e.text), step_name(s));
    copy_text(e.request_id, sizeof(e.request_id), request_id_);
    e.slot = slot_;
    pub.publish(e);
}

void Dispenser::end_run(Outcome o, const char* detail, Publisher& pub) {
    if (state_ != State::Running) return;
    if (o != Outcome::Complete) memset(on_, 0, sizeof(on_));
    state_ = State::Done;
    Event e = {};
    e.type = EventType::Outcome;
    e.value = static_cast<uint32_t>(o);
    copy_text(e.text, sizeof(e.text), detail);
    copy_text(e.request_id, sizeof(e.request_id), request_id_);
    e.slot = slot_;
    pub.publish(e);
}

bool Dispenser::start(const Command& cmd, uint32_t now_ms) {
    if (state_ != State::Idle) return false;
    p_ = cmd.profile;
    copy_text(request_id_, sizeof(request_id_), cmd.request_id);
    slot_ = cmd.slot;
    memset(on_, 0, sizeof(on_));
    first_actuator_on_ = false;
    fill_detail_[0] = '\0';
    fill_empty_ = false;
    agitate_ms_ = to_ms(p_.agitate.run_s);
    fill_ms_ = to_ms(p_.ice.max_run_s);
    drop_ms_ = to_ms(p_.release.drop_s);
    pulse_ms_ = to_ms(p_.release.pulse_s);
    open_ms_ = to_ms(p_.release.open_timeout_s);
    close_ms_ = to_ms(p_.release.close_timeout_s);
    for (int i = 0; i < 4; ++i) {
        acc_state_[i] = AccState::Pending;
        acc_armed_[i] = false;
        acc_off_at_[i] = 0;
        acc_lag_ms_[i] = i < p_.acc_count ? to_ms(p_.acc[i].lag_s) : 0;
    }
    state_ = State::Running;
    phase_start_ = now_ms;
    if (cmd.mechanism == Mechanism::WaterFill || p_.mechanism == Mechanism::WaterFill) {
        phase_ = Phase::Water;  // Task 6 replaces this stub
        return true;
    }
    if (p_.release.proof == Release::ByCatch) set_out(p_.release.channel, true);
    const uint8_t first_bit = p_.agitate.motor >= 0 ? kBitAgitate : kBitFill;
    lead_ms_ = 0;
    begin_step(first_bit);
    for (int i = 0; i < p_.acc_count; ++i) {
        if ((p_.acc[i].on_during_mask & first_bit) && to_ms(p_.acc[i].lead_s) > lead_ms_) {
            lead_ms_ = to_ms(p_.acc[i].lead_s);
        }
    }
    phase_ = Phase::Lead;
    return true;
}

void Dispenser::enter_fill(uint32_t now, Publisher& pub) {
    phase_ = Phase::Fill;
    phase_start_ = now;
    begin_step(kBitFill);
    set_out(p_.ice.motor, true);
    emit_step(Step::Fill, pub);
}

void Dispenser::enter_release(uint32_t now, Publisher& pub) {
    phase_start_ = now;
    begin_step(kBitRelease);
    emit_step(Step::Release, pub);
    if (p_.release.proof == Release::ByCatch) {
        set_out(p_.release.channel, false);
        phase_ = Phase::RelCatch;
    } else {
        set_out(p_.release.channel, true);
        phase_ = Phase::RelPulse;
    }
}

void Dispenser::tick(uint32_t now, const InputScanner& in, bool (&outputs)[kOutputCount], Publisher& pub) {
    if (state_ == State::Idle) return;
    if (state_ == State::Running) {
        // Accessory lags.
        for (int i = 0; i < p_.acc_count; ++i) {
            if (acc_state_[i] == AccState::On && acc_armed_[i] && reached(now, acc_off_at_[i])) {
                acc_state_[i] = AccState::Off;
                set_out(p_.acc[i].channel, false);
            }
        }
        for (bool again = true; again && state_ == State::Running;) {
            again = false;
            switch (phase_) {
                case Phase::Water:
                    end_run(Outcome::Error, "water fill not implemented", pub);
                    break;
                case Phase::Lead:
                    if (reached(now, phase_start_ + lead_ms_)) {
                        if (p_.agitate.motor >= 0) {
                            phase_ = Phase::Agitate;
                            phase_start_ = now;
                            set_out(p_.agitate.motor, true);
                            emit_step(Step::Agitate, pub);
                        } else {
                            enter_fill(now, pub);
                        }
                        again = true;
                    }
                    break;
                case Phase::Agitate:
                    if (reached(now, phase_start_ + agitate_ms_)) {
                        set_out(p_.agitate.motor, false);
                        end_step(kBitAgitate, now);
                        enter_fill(now, pub);
                        again = true;
                    }
                    break;
                case Phase::Fill: {
                    const bool by_sensor = p_.ice.proof == IceFill::BySensor;
                    if (by_sensor && is_binary_input(p_.ice.sensor) && in.state(in_from_index(p_.ice.sensor))) {
                        snprintf(fill_detail_, sizeof(fill_detail_), "bag_full after %.1f s",
                                 static_cast<double>(now - phase_start_) / 1000.0);
                    } else if (reached(now, phase_start_ + fill_ms_)) {
                        copy_text(fill_detail_, sizeof(fill_detail_), "max_run_s reached");
                        if (by_sensor) fill_empty_ = true;
                    } else {
                        break;
                    }
                    set_out(p_.ice.motor, false);
                    if (fill_empty_) {
                        end_run(Outcome::BinEmpty, fill_detail_, pub);
                        break;
                    }
                    end_step(kBitFill, now);
                    enter_release(now, pub);
                    again = true;
                    break;
                }
                case Phase::RelCatch:
                    if (reached(now, phase_start_ + drop_ms_)) {
                        end_step(kBitRelease, now);
                        phase_ = Phase::Tail;
                        again = true;
                    }
                    break;
                case Phase::RelPulse:
                    if (reached(now, phase_start_ + pulse_ms_)) {
                        set_out(p_.release.channel, false);
                        if (p_.release.proof == Release::BySensor) {
                            phase_ = Phase::RelOpen;
                            phase_start_ = now;
                        } else {
                            end_step(kBitRelease, now);
                            phase_ = Phase::Tail;
                        }
                        again = true;
                    }
                    break;
                case Phase::RelOpen:
                    if (is_binary_input(p_.release.sensor) && in.state(in_from_index(p_.release.sensor))) {
                        phase_ = Phase::RelClose;
                        phase_start_ = now;
                        again = true;
                    } else if (reached(now, phase_start_ + open_ms_)) {
                        end_run(Outcome::DoorOpen, "door did not open", pub);
                    }
                    break;
                case Phase::RelClose:
                    if (!(is_binary_input(p_.release.sensor) && in.state(in_from_index(p_.release.sensor)))) {
                        end_step(kBitRelease, now);
                        phase_ = Phase::Tail;
                        again = true;
                    } else if (reached(now, phase_start_ + close_ms_)) {
                        end_run(Outcome::DoorOpen, "door did not close", pub);
                    }
                    break;
                case Phase::Tail: {
                    bool pending = false;
                    for (int i = 0; i < p_.acc_count; ++i) pending |= acc_state_[i] == AccState::On;
                    if (!pending) end_run(Outcome::Complete, fill_detail_, pub);
                    break;
                }
            }
        }
    }
    // Running or Done: drive every output from the internal image.
    for (int i = 0; i < kOutputCount; ++i) outputs[i] = on_[i];
    if (!first_actuator_on_) {
        for (int i = 0; i < kOutputCount; ++i) first_actuator_on_ |= on_[i];
    }
}

void Dispenser::abort(uint32_t, const char* detail, Publisher& pub) { end_run(Outcome::Error, detail, pub); }

void Dispenser::finish() {
    if (state_ == State::Done) state_ = State::Idle;
}

}  // namespace vendcore
