// MDB cashless reader (level 1, plus the level 3 always-idle option). One bus transaction per step(); no heap.
#pragma once

#include <stdint.h>

#include "device_state.h"
#include "events.h"
#include "link.h"
#include "ports.h"

namespace mdbcore {

class Cashless {
public:
    enum class Phase : uint8_t {
        Inactive, Reset, SetupConfig, SetupPrices, RequestId, FeatureEnable, ReaderEnable, Polling
    };

    Cashless(Link& link, Clock& clock, EventSink& sink, IdSource& ids);

    void step();
    void set_enabled(bool on);  // READER enable/disable, applied on the bus from the next step
    void reset();               // (re)starts the whole init ladder; an open session is ended
    // Ask for always-idle (level 3 readers that offer it). Takes effect at the next reset().
    void set_always_idle_wanted(bool on) { always_idle_wanted_ = on; }
    // True with the open session's id (16 hex + NUL, so out needs 17 bytes). funds_cents is 0 when unknown.
    bool session_open(char* session_id_out, bool& funds_known, uint32_t& funds_cents) const;
    // False unless a session is open (or always-idle is active) and no other vend step is outstanding.
    // A price that is not a multiple of the reader's scale factor is answered locally with a
    // VendDenied{"price_scale"} event (returns true; nothing is sent on the bus).
    bool vend_request(uint32_t cents, uint16_t item, const char* request_id);
    bool vend_result(bool success, uint16_t item);  // false unless a vend is approved and open
    bool end_session();                             // false when no session is open or a vend step is outstanding
    bool cash_sale(uint32_t cents, uint16_t item);  // false unless polling, idle and the price fits the scale
    void set_edge_timeout_ms(uint32_t ms) { edge_timeout_ms_ = ms; }
    bool enable_pending() const { return enable_dirty_; }  // a READER enable/disable is not ACKed yet
    // True once the reader ACKed the VEND REQUEST of the current request (false again after a denial).
    bool vend_command_sent() const { return vend_sent_; }
    // A vend is held by the reader: requested (queued or sent) or approved and awaiting its result.
    bool vend_active() const { return vend_ != Vend::None; }
    // A vend_result / end_session action chain is still queued (not every command ACKed yet).
    bool action_pending() const { return action_ != Action::None; }
    // The reader NAKed a command of the chain started by the last vend_result / end_session.
    bool action_nak() const { return action_nak_; }
    bool always_idle() const { return always_idle_active_; }
    const DeviceState& state() const { return state_; }
    Phase phase() const { return phase_; }
    uint32_t dropped_events() const { return dropped_events_; }

private:
    static constexpr uint8_t kAddr = 0x10;
    static constexpr uint32_t kOfflineRetryMs = 1000;
    static constexpr uint8_t kPending = 16;

    enum class Action : uint8_t {
        None, VendRequest, VendCancel, VendSuccess, VendFailure, SessionComplete
    };
    enum class Vend : uint8_t { None, Requested, Approved };

    void do_reset();
    void do_setup_config();
    void do_setup_prices();
    void do_request_id();
    void do_feature_enable();
    void do_reader_enable();
    void do_action();
    void do_cash_sale();
    void do_poll();
    void note_miss();
    void push(const PaymentEvent& e);
    bool flush();
    void update_status(Status s, int16_t mdb, const char* detail, bool force = false);
    Status base_status() const { return enabled_ ? Status::Ready : Status::Disabled; }
    PaymentEvent session_event(EventKind kind) const;
    void emit_denied(const char* reason);
    void deny_vend(const char* reason);
    void lose_session(SessionEndReason why);
    void clear_session();
    void queue(Action first, Action then);
    void begin_session(uint16_t funds_units);
    void enter_polling();

    Link& link_;
    Clock& clock_;
    EventSink& sink_;
    IdSource& ids_;
    Phase phase_ = Phase::Inactive;
    DeviceState state_{};
    uint32_t edge_timeout_ms_ = 30000;
    uint32_t max_response_ms_ = 5000;
    uint16_t scale_ = 1;
    uint8_t vmc_level_ = 1;
    bool enabled_ = false;
    bool enable_dirty_ = true;
    bool swallow_reset_ = false;
    bool offline_reported_ = false;
    bool always_idle_wanted_ = false;
    bool always_idle_offered_ = false;
    bool always_idle_active_ = false;
    uint32_t last_attempt_ms_ = 0;

    bool session_open_ = false;
    bool idle_vend_ = false;  // the "session" exists only because an always-idle vend needed an id
    bool closing_ = false;
    bool funds_known_ = false;
    uint32_t funds_cents_ = 0;
    SessionEndReason end_reason_ = SessionEndReason::Reader;
    char session_id_[17] = {};
    Vend vend_ = Vend::None;
    bool vend_sent_ = false;
    uint32_t vend_cents_ = 0;
    uint16_t vend_item_ = 0;
    uint16_t result_item_ = 0;
    char request_id_[65] = {};
    uint32_t approved_ms_ = 0;
    Action action_ = Action::None;
    Action follow_ = Action::None;
    bool action_nak_ = false;
    bool cash_pending_ = false;
    uint32_t cash_cents_ = 0;
    uint16_t cash_item_ = 0;

    PaymentEvent pending_[kPending];
    uint8_t n_pending_ = 0;
    uint32_t dropped_events_ = 0;
};

}  // namespace mdbcore
