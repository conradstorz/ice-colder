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
    bound_ms_ = to_ms(worst_case_seconds(p_)) + 1000;
    memset(on_, 0, sizeof(on_));
    first_actuator_on_ = false;
    fill_detail_[0] = '\0';
    fill_empty_ = false;
    agitate_ms_ = to_ms(p_.agitate.run_s);
    fill_ms_ = to_ms(p_.ice.max_run_s);
    water_ms_ = to_ms(p_.water.max_fill_s);
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
    const bool water = p_.mechanism == Mechanism::WaterFill;
    if (!water && p_.release.proof == Release::ByCatch) set_out(p_.release.channel, true);
    const uint8_t first_bit = !water && p_.agitate.motor >= 0 ? kBitAgitate : kBitFill;
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

void Dispenser::enter_water(uint32_t now, const InputScanner& in, Publisher& pub) {
    phase_ = Phase::WFill;
    phase_start_ = now;
    begin_step(kBitFill);
    set_out(p_.water.valve, true);
    emit_step(Step::Fill, pub);
    memset(buckets_, 0, sizeof(buckets_));
    pulse_base_ = in.pulses();
    last_delta_ = 0;
    last_pulse_ms_ = now;
    next_tel_ms_ = now + 1000;
    below_ = false;
    const float ppl = p_.water.pulses_per_l;
    target_pulses_ = static_cast<uint32_t>(p_.water.target_ml * ppl / 1000.0f + 0.5f);
    limit_pulses_ = static_cast<uint32_t>(static_cast<float>(target_pulses_) * (1.0f + p_.water.over_pct / 100.0f));
}

void Dispenser::water_valve_off(uint32_t now) {
    set_out(p_.water.valve, false);
    end_step(kBitFill, now);
}

// Pulses seen in the trailing ~1000 ms: delta minus the base, where the base is the newest bucket at
// least 100 epochs (1000 ms) old, else the oldest valid earlier bucket, else the fill start (0).
uint32_t Dispenser::window_pulses(uint32_t now, uint32_t delta) const {
    const uint32_t cur = now / kBucketMs;
    bool have_old = false, have_any = false;
    uint32_t old_age = 0, any_age = 0, old_delta = 0, any_delta = 0;
    for (uint32_t i = 0; i < kBuckets; ++i) {
        const Bucket& b = buckets_[i];
        if (!b.used) continue;
        const uint32_t age = cur - b.epoch;
        if (age >= kBuckets || age == 0) continue;  // stale (previous lap) or the current tick's bucket
        if (age >= 100u && (!have_old || age < old_age)) {
            have_old = true;
            old_age = age;
            old_delta = b.delta;
        }
        if (!have_any || age > any_age) {
            have_any = true;
            any_age = age;
            any_delta = b.delta;
        }
    }
    return delta - (have_old ? old_delta : have_any ? any_delta : 0u);
}

void Dispenser::water_fill_tick(uint32_t now, const InputScanner& in, Publisher& pub, bool& again) {
    if (p_.water.proof == WaterFill::Timed) {
        if (reached(now, phase_start_ + water_ms_)) {
            copy_text(fill_detail_, sizeof(fill_detail_), "max_fill_s reached");
            water_valve_off(now);
            phase_ = Phase::Tail;
            again = true;
        }
        return;
    }
    const uint32_t delta = in.pulses() - pulse_base_;
    if (phase_ == Phase::WSettle) {
        if (delta != last_delta_) {
            last_delta_ = delta;
            last_pulse_ms_ = now;
        }
        if (delta > limit_pulses_) {
            char d[48];
            snprintf(d, sizeof(d), "%lu pulses, target %lu", static_cast<unsigned long>(delta),
                     static_cast<unsigned long>(target_pulses_));
            end_run(Outcome::OverDispense, d, pub);
        } else if (reached(now, last_pulse_ms_ + 1000u) || reached(now, settle_start_ms_ + kSettleMaxMs)) {
            snprintf(fill_detail_, sizeof(fill_detail_), "%lu pulses", static_cast<unsigned long>(delta));
            phase_ = Phase::Tail;
            again = true;
        }
        return;
    }
    buckets_[(now / kBucketMs) % kBuckets] = {now / kBucketMs, delta, true};
    last_delta_ = delta;
    if (delta >= target_pulses_) {
        water_valve_off(now);
        last_pulse_ms_ = now;
        settle_start_ms_ = now;
        phase_ = Phase::WSettle;
        again = true;
        return;
    }
    if (reached(now, phase_start_ + water_ms_)) {
        end_run(Outcome::Timeout, "max_fill_s reached", pub);
        return;
    }
    const float ppl = p_.water.pulses_per_l;
    const float rate = ppl > 0.0f ? static_cast<float>(window_pulses(now, delta)) * 1000.0f / ppl : 0.0f;
    if (rate < p_.water.min_flow_ml_s) {
        if (!below_) {
            below_ = true;
            below_since_ms_ = now;
        }
        if (now - below_since_ms_ > to_ms(p_.water.no_flow_grace_s)) {
            end_run(Outcome::NoFlow, "no flow", pub);
            return;
        }
    } else {
        below_ = false;
    }
    if (reached(now, next_tel_ms_)) {
        next_tel_ms_ += 1000;
        Event e = {};
        e.type = EventType::Telemetry;
        e.channel = static_cast<uint8_t>(channel_index("fill_pulses"));
        e.value = delta;
        copy_text(e.request_id, sizeof(e.request_id), request_id_);
        e.slot = slot_;
        pub.publish(e);
    }
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

void Dispenser::tick(uint32_t now, const InputScanner& in, OutputGuard& guard, Publisher& pub) {
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
                case Phase::WFill:
                case Phase::WSettle:
                    water_fill_tick(now, in, pub, again);
                    break;
                case Phase::Lead:
                    if (reached(now, phase_start_ + lead_ms_)) {
                        if (p_.mechanism == Mechanism::WaterFill) {
                            enter_water(now, in, pub);
                        } else if (p_.agitate.motor >= 0) {
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
                // BySensor release. Timeout origins: the open timeout runs from the end of the
                // solenoid pulse (phase_start_ set there); the close timeout runs from the tick
                // the door was first seen open. A door that never opens means no release happened,
                // so it is a dispense Error (the edge treats door_open as "bag released, door stuck
                // open" and credits the sale); a door that opens but never closes is DoorOpen.
                case Phase::RelOpen:
                    if (is_binary_input(p_.release.sensor) && in.state(in_from_index(p_.release.sensor))) {
                        phase_ = Phase::RelClose;
                        phase_start_ = now;
                        again = true;
                    } else if (reached(now, phase_start_ + open_ms_)) {
                        end_run(Outcome::Error, "door did not open", pub);
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
    // Running or Done: request the outputs this machine owns from the internal image.
    request_all(now, guard);
}

void Dispenser::request_all(uint32_t now, OutputGuard& guard) {
    static constexpr Out kOwned[5] = {Out::Agitator, Out::Auger, Out::BagFan, Out::TrapDoorCatch, Out::WaterValve};
    for (Out o : kOwned) {
        const bool on = on_[static_cast<int>(o)];
        guard.request(o, on, bound_ms_, now);
        if (on) first_actuator_on_ = true;
    }
}

void Dispenser::abort(uint32_t now, const char* detail, OutputGuard& guard, Publisher& pub) {
    end_run(Outcome::Error, detail, pub);
    if (state_ != State::Idle) request_all(now, guard);
}

void Dispenser::finish() {
    if (state_ == State::Done) state_ = State::Idle;
}

}  // namespace vendcore
