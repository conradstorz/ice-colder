#include "core.h"

#include <stdio.h>
#include <string.h>

namespace vendcore {

namespace {

void copy_text(char* dst, size_t cap, const char* src) {
    if (cap == 0) return;
    if (src == nullptr) src = "";
    strncpy(dst, src, cap - 1);
    dst[cap - 1] = '\0';
}

const char* kind_name(CmdKind k) {
    switch (k) {
        case CmdKind::Ping: return kCommands[0];
        case CmdKind::SelfTest: return kCommands[1];
        case CmdKind::ForceReport: return kCommands[2];
        case CmdKind::Dispense: return kCommands[3];
        case CmdKind::WaterValve: return kCommands[4];
        default: return "";
    }
}

constexpr int kValve = static_cast<int>(Out::WaterValve);

}  // namespace

Core::Core(const CoreConfig& cfg, Clock& clock, IoPort& io, Publisher& pub)
    : cfg_(cfg),
      clock_(clock),
      io_(io),
      pub_(pub),
      scanner_(cfg.polarity),
      vmc_(cfg.silence_ms),
      ro_(cfg.ro_max_run_ms),
      freeze_(cfg.heater_on_c, cfg.heater_off_c) {}

void Core::tick() {
    const uint32_t now = clock_.now_ms();
    InputSnapshot s = {};
    io_ok_ = io_.read(s);
    if (io_ok_) raw_ = s;
    health_.set_io_ok(io_ok_);
    health_.set_deadline_met(!ticked_ || now - last_tick_ms_ <= 20);
    ticked_ = true;
    last_tick_ms_ = now;
    step(now);
    if (health_.ok()) io_.kick_watchdog();
}

void Core::on_trip(uint32_t now, const char* detail) {
    if (dispenser_.state() == Dispenser::State::Running) dispenser_.abort(now, detail, guard_, pub_);
    cancel_valve(detail);
}

void Core::step(uint32_t now) {
    step_trip_ = nullptr;
    if (!started_) {
        started_ = true;
        last_io_republish_ms_ = now;
    }
    if (io_ok_) scanner_.scan(raw_, now, pub_);

    using Trip = TripReason::Trip;
    const bool prev_active = trip_active_;
    const Trip prev_kind = trip_kind_;
    // Latching conditions come straight from the inputs, never from which rule happened to win the
    // first-match order (sump_high and master_enable_dropped alternate under heater demand).
    bool latched = !io_ok_ || scanner_.state(In::SumpHigh) ||
                   (scanner_.state(In::ServiceDoor) && guard_.any_motion());
    TripReason why = {};
    const bool tripped =
        interlocks_.trip(scanner_, raw_, io_ok_, vmc_, guard_.any_motion(), guard_.any_on(), now, why);
    bool offline_only = false;
    bool skip_ro = false;
    if (tripped) {
        const bool onset = !prev_active || prev_kind != why.kind;
        offline_only = why.kind == Trip::VmcOffline;
        skip_ro = !offline_only && why.kind != Trip::ServiceDoor;
        step_trip_ = why.detail;
        on_trip(now, why.detail);
        // vmc_offline holds for as long as the VMC is silent: force everything off at onset only, so
        // the autonomous RO/heater loops keep their own max-on timers running afterwards.
        if (!offline_only || onset) guard_.all_off();
    }

    if (!tripped || offline_only) {
        if (dispenser_.state() == Dispenser::State::Running) dispenser_.tick(now, scanner_, guard_, pub_);
        valve_tick(now);
    }
    if (skip_ro) {
        guard_.request(Out::RoProduction, false, cfg_.ro_max_run_ms + 1000, now);
    } else {
        ro_.tick(scanner_, guard_, now, pub_);
    }
    // The heater coil common runs through the master relay, so a latching interlock (which drops it)
    // leaves the heater unpowered: request it off rather than fight the relay.
    if (latched) {
        freeze_.hold_off(guard_, now);
    } else {
        freeze_.tick(raw_, guard_, now);
    }

    // Motion-conditioned rules see this step's requests only now: re-check before anything is written.
    TripReason why2 = {};
    const bool tripped2 =
        interlocks_.trip(scanner_, raw_, io_ok_, vmc_, guard_.any_motion(), guard_.any_on(), now, why2);
    if (tripped2 && (!tripped || why2.kind != why.kind)) {
        step_trip_ = why2.detail;
        on_trip(now, why2.detail);
        guard_.all_off();
    }
    if (tripped2 && (why2.kind == Trip::SumpHigh || why2.kind == Trip::ServiceDoor)) latched = true;
    health_.set_latched(latched);

    // One decision per tick: the effective trip is the second check's if it trips, else the first's.
    if (tripped || tripped2) {
        const TripReason& eff = tripped2 ? why2 : why;
        if (!prev_active || prev_kind != eff.kind) {
            Event e = {};
            e.type = EventType::Health;
            copy_text(e.text, sizeof(e.text), eff.detail);
            pub_.publish(e);
        }
        trip_active_ = true;
        trip_kind_ = eff.kind;
    } else {
        trip_active_ = false;
        trip_kind_ = Trip::None;
    }

    if (io_ok_ && raw_.ambient_valid && (!temp_sent_ || now - last_temp_ms_ >= cfg_.temp_publish_ms)) {
        publish_temperature();
        temp_sent_ = true;
        last_temp_ms_ = now;
    }
    if (now - last_io_republish_ms_ >= cfg_.io_republish_ms) {
        scanner_.republish_all(pub_);
        last_io_republish_ms_ = now;
    }
    if (force_report_) {
        force_report_ = false;
        scanner_.republish_all(pub_);
        guard_.republish_all(pub_);
        if (io_ok_ && raw_.ambient_valid) publish_temperature();
    }

    guard_.apply(now, out_, pub_);
    io_.write_outputs(out_);
    if (dispenser_.state() == Dispenser::State::Done) dispenser_.finish();
}

void Core::publish_temperature() {
    Event e = {};
    e.type = EventType::Temperature;
    e.channel = static_cast<uint8_t>(channel_index("ambient"));
    e.fvalue = raw_.ambient_c;
    pub_.publish(e);
}

bool Core::dispense_output_written() const {
    return out_[static_cast<int>(Out::Agitator)] || out_[static_cast<int>(Out::Auger)] ||
           out_[static_cast<int>(Out::BagFan)] || out_[static_cast<int>(Out::TrapDoorCatch)] || out_[kValve];
}

void Core::valve_tick(uint32_t now) {
    if (!valve_active_) return;
    if (now - valve_start_ms_ >= valve_ms_) {
        guard_.request(Out::WaterValve, false, valve_ms_ + 500, now);
        valve_active_ = false;
        Ack a = {};
        copy_text(a.request_id, sizeof(a.request_id), valve_id_);
        copy_text(a.command, sizeof(a.command), kCommands[4]);
        a.status = AckStatus::Ok;
        a.phase = Phase::Completed;
        push_completion(a);
    } else {
        guard_.request(Out::WaterValve, true, valve_ms_ + 500, now);
    }
}

void Core::cancel_valve(const char* detail) {
    if (!valve_active_) return;
    valve_active_ = false;
    Ack a = {};
    copy_text(a.request_id, sizeof(a.request_id), valve_id_);
    copy_text(a.command, sizeof(a.command), kCommands[4]);
    a.status = AckStatus::Failed;
    a.phase = Phase::Completed;
    copy_text(a.detail, sizeof(a.detail), detail);
    push_completion(a);
}

void Core::push_completion(const Ack& a) {
    if (suppress_completion_) return;
    if (ring_count_ == kRing) {  // full: drop the oldest
        ring_head_ = (ring_head_ + 1) % kRing;
        --ring_count_;
    }
    ring_[(ring_head_ + ring_count_) % kRing] = a;
    ++ring_count_;
}

bool Core::poll_completion(Ack& out) {
    if (ring_count_ == 0) return false;
    out = ring_[ring_head_];
    ring_head_ = (ring_head_ + 1) % kRing;
    --ring_count_;
    return true;
}

Ack Core::make_ack(const Command& c, AckStatus s, const char* detail) const {
    Ack a = {};
    copy_text(a.request_id, sizeof(a.request_id), c.request_id);
    copy_text(a.command, sizeof(a.command), c.name[0] != '\0' ? c.name : kind_name(c.kind));
    a.status = s;
    a.phase = Phase::Completed;
    copy_text(a.detail, sizeof(a.detail), detail);
    return a;
}

Ack Core::submit(const Decoded& d) {
    const uint32_t now = clock_.now_ms();
    const Command& c = d.cmd;
    switch (d.result) {
        case Decode::NoRequestId: return make_ack(c, AckStatus::Rejected, d.detail);
        case Decode::Rejected: return make_ack(c, AckStatus::Rejected, d.detail);
        case Decode::Unsupported: return make_ack(c, AckStatus::Unsupported, d.detail);
        case Decode::Ok: break;
    }
    switch (c.kind) {
        case CmdKind::Ping: return make_ack(c, AckStatus::Ok, "");
        case CmdKind::SelfTest: {
            Ack a = make_ack(c, AckStatus::Ok, "");
            Ack t = {};
            self_test(t);
            copy_text(a.result_json, sizeof(a.result_json), t.result_json);
            return a;
        }
        case CmdKind::ForceReport:
            force_report_ = true;
            return make_ack(c, AckStatus::Ok, "");
        case CmdKind::Dispense:
        case CmdKind::WaterValve: break;
        default: return make_ack(c, AckStatus::Unsupported, "unsupported command");
    }

    if (dispenser_.state() != Dispenser::State::Idle || valve_active_) {
        return make_ack(c, AckStatus::Rejected, "busy");
    }
    if (vmc_.silent(now)) return make_ack(c, AckStatus::Rejected, "vmc_offline");

    if (c.kind == CmdKind::Dispense) {
        dispenser_.start(c, now);
        step(now);
        if (dispense_output_written()) {
            Ack a = make_ack(c, AckStatus::Ok, "");
            a.phase = Phase::Accepted;
            return a;
        }
        if (dispenser_.state() == Dispenser::State::Running) {  // never energized: end the run
            dispenser_.abort(now, "actuator_not_started", guard_, pub_);
            dispenser_.finish();
            return make_ack(c, AckStatus::Failed, "actuator_not_started");
        }
        return make_ack(c, AckStatus::Failed, step_trip_ != nullptr ? step_trip_ : "failed");
    }

    suppress_completion_ = true;
    valve_active_ = true;
    copy_text(valve_id_, sizeof(valve_id_), c.request_id);
    valve_start_ms_ = now;
    valve_ms_ = static_cast<uint32_t>(c.valve_seconds) * 1000u;
    step(now);
    if (valve_active_ && out_[kValve]) {
        suppress_completion_ = false;
        Ack a = make_ack(c, AckStatus::Ok, "");
        a.phase = Phase::Accepted;
        return a;
    }
    // Ended in the same step: the caller gets the failure here, not again through poll_completion.
    const char* why = step_trip_ != nullptr ? step_trip_ : "actuator_not_started";
    if (valve_active_) cancel_valve(why);
    suppress_completion_ = false;
    return make_ack(c, AckStatus::Failed, why);
}

void Core::self_test(Ack& out) const {
    const uint32_t now = clock_.now_ms();
    struct Check {
        const char* name;
        bool pass;
        char detail[24];
    } checks[6] = {};
    auto fill = [&](int i, const char* name, bool pass, const char* detail) {
        checks[i].name = name;
        checks[i].pass = pass;
        copy_text(checks[i].detail, sizeof(checks[i].detail), detail);
    };
    fill(0, "i2c_expanders", io_ok_, io_ok_ ? "read ok" : "read failed");
    fill(1, "ambient_sensor", raw_.ambient_valid, raw_.ambient_valid ? "valid" : "no reading");
    fill(2, "watchdog_kick", health_.ok(), health_.ok() ? "kicking" : "withheld");
    const bool seen = !vmc_.silent(now);
    fill(3, "vmc_seen", seen, seen ? "online" : "silent");
    fill(4, "master_enable", raw_.master_enable, raw_.master_enable ? "enabled" : "dropped");
    char dropped[24];
    snprintf(dropped, sizeof(dropped), "dropped %u", static_cast<unsigned>(pub_.dropped));
    fill(5, "dropped_events", pub_.dropped == 0, dropped);

    out = Ack{};
    copy_text(out.command, sizeof(out.command), kCommands[1]);
    out.status = AckStatus::Ok;
    out.phase = Phase::Completed;
    size_t n = static_cast<size_t>(snprintf(out.result_json, sizeof(out.result_json), "{\"checks\":["));
    for (int i = 0; i < 6; ++i) {
        if (n >= sizeof(out.result_json) - 1) break;
        n += static_cast<size_t>(snprintf(out.result_json + n, sizeof(out.result_json) - n,
                                          "%s{\"name\":\"%s\",\"pass\":%s,\"detail\":\"%s\"}", i ? "," : "",
                                          checks[i].name, checks[i].pass ? "true" : "false", checks[i].detail));
        if (n > sizeof(out.result_json) - 1) n = sizeof(out.result_json) - 1;
    }
    if (n < sizeof(out.result_json)) snprintf(out.result_json + n, sizeof(out.result_json) - n, "]}");
}

}  // namespace vendcore
