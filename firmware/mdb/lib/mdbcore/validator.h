// MDB bill validator (level 1 and 2). One bus transaction per step(); no heap.
#pragma once

#include <stdint.h>

#include "device_state.h"
#include "events.h"
#include "link.h"
#include "ports.h"

namespace mdbcore {

struct EscrowVerdict {
    bool stack;
    BillReturnReason reason;  // used when stack is false
};

// The Master implements this from the policy and accounts; the validator never sees the policy.
struct EscrowJudge {
    virtual EscrowVerdict judge(uint32_t bill_cents) = 0;
};

class Validator {
public:
    enum class Phase : uint8_t { Inactive, Reset, Setup, Identify, Stacker, BillType, Polling };

    Validator(Link& link, Clock& clock, EventSink& sink, EscrowJudge& judge);

    void step();
    // accept=false disables every bill (zero masks). cents lists the denominations to accept.
    void set_enabled(const uint32_t* cents, uint8_t n, bool accept);
    void reset();
    bool return_escrow(BillReturnReason reason);  // false when no bill is held
    bool bill_in_escrow(uint32_t& cents) const {
        cents = held_cents_;
        return held_;
    }
    const DeviceState& state() const { return state_; }
    bool stacker(uint16_t& count, bool& full) const {
        count = stacker_count_;
        full = stacker_full_;
        return stacker_known_;
    }
    Phase phase() const { return phase_; }
    uint32_t dropped_events() const { return dropped_events_; }

private:
    static constexpr uint8_t kAddr = 0x30;
    static constexpr uint32_t kStackerRefreshMs = 10000;
    static constexpr uint32_t kOfflineRetryMs = 1000;
    static constexpr uint8_t kPending = 16;

    enum class EscrowCmd : uint8_t { None, Judge, Stack, Return };

    void do_reset();
    void do_setup();
    void do_identify();
    void do_stacker();
    void do_bill_type();
    void do_escrow();
    void do_poll();
    void note_miss();
    void push(const PaymentEvent& e);
    bool flush();
    void update_status(Status s, int16_t mdb, const char* detail, bool force = false);
    Status base_status() const;
    uint16_t enable_mask() const;
    uint32_t type_cents(uint8_t type) const { return static_cast<uint32_t>(credit_[type]) * scaling_; }
    bool type_valid(uint8_t type) const { return credit_[type] != 0; }
    void enter_polling();
    void clear_escrow();
    void lose_escrow();
    void bill_event(EventKind kind, uint32_t cents, BillReturnReason reason);

    Link& link_;
    Clock& clock_;
    EventSink& sink_;
    EscrowJudge& judge_;
    Phase phase_ = Phase::Inactive;
    DeviceState state_{};
    uint16_t scaling_ = 1;
    uint8_t credit_[16] = {};
    bool escrow_capable_ = false;
    bool accept_ = false;
    bool mask_dirty_ = true;
    bool swallow_reset_ = false;
    bool offline_reported_ = false;
    bool no_escrow_reported_ = false;
    bool cashbox_out_ = false;
    bool stacker_known_ = false;
    bool stacker_full_ = false;
    bool stacker_due_ = false;
    uint16_t stacker_count_ = 0;
    uint32_t last_stacker_ms_ = 0;
    uint32_t last_attempt_ms_ = 0;
    bool held_ = false;
    uint32_t held_cents_ = 0;
    EscrowCmd escrow_cmd_ = EscrowCmd::None;
    bool escrow_sent_ = false;
    bool return_requested_ = false;
    BillReturnReason return_reason_ = BillReturnReason::Policy;
    PaymentEvent pending_[kPending];
    uint8_t n_pending_ = 0;
    uint32_t dropped_events_ = 0;
};

}  // namespace mdbcore
