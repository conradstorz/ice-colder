// Dispenser state machine. Task 5: bagged ice. Task 6 adds water fill.
#pragma once

#include <stdint.h>

#include "channels.h"
#include "command.h"
#include "inputs.h"
#include "ports.h"
#include "profile.h"

namespace vendcore {

enum class Step : uint8_t { Agitate, Fill, Release };
enum class Outcome : uint8_t { Complete, BinEmpty, Timeout, Jam, Error, DoorOpen, NoFlow, OverDispense };

// Contract strings (DispenseStep / DispenserOutcome in vending_machine.py).
const char* step_name(Step s);
const char* outcome_name(Outcome o);

class Dispenser {
public:
    enum class State : uint8_t { Idle, Running, Done };

    // False unless Idle (a Done run must be finish()ed first).
    bool start(const Command& cmd, uint32_t now_ms);
    // Writes all seven outputs while Running or Done; leaves them alone when Idle.
    // The Outcome event carries the Outcome ordinal in `value` and the detail in `text`.
    void tick(uint32_t now_ms, const InputScanner& in, bool (&outputs)[kOutputCount], Publisher& pub);
    void abort(uint32_t now_ms, const char* detail, Publisher& pub);  // Outcome::Error, all off
    bool first_actuator_on() const { return first_actuator_on_; }     // for the accepted ack
    State state() const { return state_; }
    const char* request_id() const { return request_id_; }
    uint8_t slot() const { return slot_; }
    void finish();  // Done -> Idle

private:
    enum class Phase : uint8_t { Lead, Agitate, Fill, RelCatch, RelPulse, RelOpen, RelClose, Tail, Water };
    enum class AccState : uint8_t { Pending, On, Off };

    void set_out(int channel, bool on);
    void begin_step(uint8_t bit);
    void end_step(uint8_t bit, uint32_t now);
    void enter_fill(uint32_t now, Publisher& pub);
    void enter_release(uint32_t now, Publisher& pub);
    void emit_step(Step s, Publisher& pub);
    void end_run(Outcome o, const char* detail, Publisher& pub);

    State state_ = State::Idle;
    Phase phase_ = Phase::Lead;
    SlotProfile p_ = {};
    char request_id_[65] = {};
    uint8_t slot_ = 0;
    bool on_[kOutputCount] = {};
    bool first_actuator_on_ = false;

    uint32_t phase_start_ = 0;
    uint32_t lead_ms_ = 0, agitate_ms_ = 0, fill_ms_ = 0, drop_ms_ = 0, pulse_ms_ = 0;
    uint32_t open_ms_ = 0, close_ms_ = 0;
    uint32_t acc_lag_ms_[4] = {};
    AccState acc_state_[4] = {};
    bool acc_armed_[4] = {};
    uint32_t acc_off_at_[4] = {};
    char fill_detail_[48] = {};
    bool fill_empty_ = false;
};

}  // namespace vendcore
