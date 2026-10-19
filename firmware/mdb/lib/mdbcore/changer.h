// MDB coin changer (level 2 and 3). One bus transaction per step(); no heap.
#pragma once

#include <stdint.h>

#include "device_state.h"
#include "events.h"
#include "link.h"
#include "ports.h"

namespace mdbcore {

class Changer {
public:
    enum class Phase : uint8_t { Inactive, Reset, Setup, Identify, Features, TubeStatus, CoinType, Polling };

    Changer(Link& link, Clock& clock, EventSink& sink);

    void step();
    // accept=false disables every coin (zero mask). cents lists the denominations the policy wants;
    // state().enabled is that list intersected with the denominations the changer reported in SETUP
    // (empty until then), and only those can ever reach the COIN TYPE mask.
    void set_enabled(const uint32_t* cents, uint8_t n, bool accept);
    void reset();
    // False when the payout is impossible: offline, one already running, or fewer cents than one
    // scaled unit. A level 3 request above 255 units is sent in chunks; an amount that is not a
    // multiple of the scaling pays the floor multiple and ends partial with detail "scaling".
    bool request_payout(uint32_t cents, const char* request_id);
    bool payout_in_progress() const { return payout_.active; }
    // True once the changer has ACKed the first PAYOUT (level 3) or DISPENSE (level 2) command.
    bool payout_acked() const { return payout_.active && payout_.acked; }
    // A coin-type (enable) update has not been ACKed on the bus yet.
    bool enable_pending() const { return mask_dirty_; }
    const DeviceState& state() const { return state_; }
    const TubeLevel* tubes(uint8_t& n) const { n = n_tubes_; return tubes_; }
    uint32_t payable_total_cents() const;
    bool exact_change(uint32_t threshold_cents) const { return payable_total_cents() < threshold_cents; }
    bool escrow_lever_pending() { bool v = lever_; lever_ = false; return v; }
    Phase phase() const { return phase_; }
    uint32_t dropped_events() const { return dropped_events_; }

private:
    static constexpr uint8_t kAddr = 0x08;
    static constexpr uint32_t kTubeRefreshMs = 10000;
    static constexpr uint32_t kOfflineRetryMs = 1000;
    static constexpr uint32_t kPayoutTimeoutMs = 30000;
    static constexpr uint8_t kPending = 16;

    enum class PayStep : uint8_t { L3Send, L3Value, L3Status, L2Dispense, L2Refresh };

    struct Payout {
        bool active;
        bool acked;
        bool level3;
        PayStep step;
        uint32_t requested;
        uint32_t target;       // cents the changer can express (requested floored to the scaling)
        uint32_t sent_cents;   // level 3: value of the chunks sent so far, the current one included
        uint32_t chunk_cents;  // level 3: value of the current chunk
        uint32_t done_cents;   // level 3: paid by the finished chunks
        uint32_t remaining;  // level 2 only
        uint32_t start_ms;
        uint32_t progress_cents;  // level 3 value-poll progress
        uint16_t before[16];  // tube counts at start (level 2)
        uint16_t working[16];
        char request_id[65];
    };

    // Return false from a handler when the transaction should be retried (no state change).
    void do_reset();
    void do_setup();
    void do_identify();
    void do_features();
    void do_tube_status();
    void do_coin_type();
    void do_poll();
    void do_payout();
    void note_miss();
    bool parse_tube_response(const Response& r);
    void rebuild_tubes();
    void push(const PaymentEvent& e);
    bool flush();
    void update_status(Status s, int16_t mdb, const char* detail);
    Status base_status() const;
    void finish_payout(uint32_t paid, const char* detail = "");
    void interrupt_payout(const char* cause);
    uint16_t enable_mask() const;
    void refilter();
    uint32_t type_cents(uint8_t type) const { return static_cast<uint32_t>(credit_[type]) * scaling_; }
    bool type_valid(uint8_t type) const { return credit_[type] != 0 && credit_[type] != 0xFF; }
    bool type_tubed(uint8_t type) const { return type_valid(type) && ((routing_ >> type) & 1); }
    void enter_polling();

    Link& link_;
    Clock& clock_;
    EventSink& sink_;
    Phase phase_ = Phase::Inactive;
    DeviceState state_{};
    uint8_t scaling_ = 1;
    uint16_t routing_ = 0;
    uint8_t credit_[16] = {};
    uint16_t full_mask_ = 0;
    uint16_t counts_[16] = {};
    TubeLevel tubes_[16] = {};
    uint8_t tube_type_[16] = {};  // coin type of tubes_[i]
    uint8_t n_tubes_ = 0;
    bool alt_payout_ = false;
    bool accept_ = false;
    uint32_t want_[16] = {};  // the policy's list as received, before the denominations filter
    uint8_t n_want_ = 0;
    bool mask_dirty_ = true;
    bool lever_ = false;
    bool tubes_due_ = false;
    bool swallow_reset_ = false;
    bool offline_reported_ = false;
    uint32_t last_tube_ms_ = 0;
    uint32_t last_attempt_ms_ = 0;
    Payout payout_{};
    PaymentEvent pending_[kPending];
    uint8_t n_pending_ = 0;
    uint32_t dropped_events_ = 0;
};

}  // namespace mdbcore
