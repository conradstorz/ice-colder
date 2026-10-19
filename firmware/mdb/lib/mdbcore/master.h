// The MDB master: composes the changer, validator and reader under the edge policy and owns
// every money decision. Pure C++, no heap; the ESP-IDF task only calls step()/submit().
//
// Wiring. The drivers must be constructed with a MasterTap (event sink + escrow judge) because
// the Master is built after them; bind the tap to the Master once it exists:
//     MasterTap tap;  Changer c(link, clock, tap); Validator v(link, clock, tap, tap); ...
//     Master m(link, clock, ids, out_sink, c, v, r, accounts, edge);  tap.bind(m);
// Until bound the tap refuses every event (the drivers hold them) and returns every bill.
//
// Acks. submit() always returns the real first ack, as vendcore::Core::submit does: the
// commands that need the bus drive the affected device for a bounded number of transactions
// inside submit() and answer from what the peripheral did:
//   * payout / vend_request answer `accepted` while the driver holds the command (queued, in
//     flight, or ended with its result held back by a full sink). `failed` is final (rule 5), so it
//     is never answered for a command the driver still holds; a terminal failure arrives as the
//     payout_result / vend_denied event. A refusal is `rejected` (`busy` only when a payout is
//     running, `unpayable` when the changer cannot express the amount at all);
//   * set_policy answers `ok` only after every present, polling device ACKed its enable update,
//     else `failed`, and a failed set_policy changes nothing (no commit, no lifted silence);
//   * vend_result / end_session / return_escrow answer `ok` once the bus confirmed, `failed` on a
//     NAK, and `ok` with detail "queued" if the action is still queued after the settle rounds.
// So there is no late ack and poll_completion() is a reserved, always-empty seam (the dispatcher
// treats a phase=accepted ack without a registered completion timeout as an error, which
// set_policy must never produce). Long-running outcomes arrive as payout_result / vend_* events.
#pragma once

#include <stdint.h>

#include "accounts.h"
#include "boardcore/ack_cache.h"
#include "cashless.h"
#include "changer.h"
#include "edge_watch.h"
#include "event_log.h"
#include "events.h"
#include "link.h"
#include "policy.h"
#include "ports.h"
#include "state.h"
#include "validator.h"

namespace mdbcore {

enum class CmdKind : uint8_t {
    Ping, SelfTest, ForceReport, SetPolicy, Payout, VendRequest, VendResult, EndSession,
    ReturnEscrow, DeviceReset, CashSale, Unknown
};

struct Command {
    CmdKind kind;
    char request_id[65];
    char name[33];
    int64_t timestamp_unix;  // -1 = unknown
    Policy policy;
    uint32_t cents;
    char reason[20];
    char sale_id[65];
    bool has_session_id;
    char session_id[65];
    char item[65];
    char vend_request_id[65];
    bool success;
    Device device;
};

class Master;

// Forwards the drivers' events and escrow questions to the Master once bound.
class MasterTap : public EventSink, public EscrowJudge {
public:
    void bind(Master& m) { master_ = &m; }
    bool emit(const PaymentEvent& e) override;
    EscrowVerdict judge(uint32_t bill_cents) override;

private:
    Master* master_ = nullptr;
};

class Master : public EventSink, public EscrowJudge {
public:
    Master(Link& link, Clock& clock, IdSource& ids, EventSink& sink, Changer& changer,
           Validator& validator, Cashless& cashless, Accounts& accounts, EdgeWatch& edge);

    // One bus transaction per device (changer, validator, reader), plus the edge watch.
    void step();
    boardcore::Ack submit(const Command& cmd);
    bool poll_completion(boardcore::Ack& out);  // reserved: see the header comment, always false
    void edge_seen() { edge_.seen(); }          // any cmd/mdb traffic; never lifts an inhibit
    void force_report() { dirty_ = true; }
    bool take_state_dirty() {
        const bool d = dirty_;
        dirty_ = false;
        return d;
    }
    void snapshot(PaymentState& out) const;
    void self_test(boardcore::Ack& out) const;

    // Unix time now, from SNTP; lets the Master age-check payout/vend_request timestamps. With
    // none ever set, the expiry rule is skipped.
    void set_wall_time(int64_t unix_now);
    // Optional: the replay log, for the self_test depth check.
    void set_event_log(const EventLog* log) { log_ = log; }

    // EventSink (from the tap): books the event into Accounts after the outer sink took it.
    bool emit(const PaymentEvent& e) override;
    // EscrowJudge (from the tap).
    EscrowVerdict judge(uint32_t bill_cents) override;

    // Replay-log pressure from the ledger (EventLog::next_pressure). While set, and no higher inhibit
    // applies, acceptance is off with Inhibit::DeviceFault: the three devices are disabled on the bus
    // like the silence sequence, but no bill is returned and no session ended. Releasing it never
    // lifts NoPolicy, EdgeSilent or PolicyOff.
    void set_log_pressure(bool on);

    Inhibit inhibit() const { return inhibit_; }

private:
    static constexpr int kSettleRounds = 8;

    struct Approved {
        bool active;
        char session_id[65];
        char request_id[65];
        uint16_t item;
    };

    bool accepting() const { return has_policy_ && policy_.accept && !silent_ && !log_pressure_; }
    void recompute_inhibit();
    void apply_enables();
    void after_changer_step();
    void step_changer();
    void step_validator();
    void step_cashless();
    void on_silence();
    void apply(const PaymentEvent& e);
    uint64_t now_unix() const;
    bool expired(const Command& c) const;
    uint32_t timeout_s() const { return has_policy_ ? policy_.edge_timeout_s : 30; }
    uint32_t state_signature() const;

    boardcore::Ack settle_cashless_action(const Command& c);
    boardcore::Ack do_set_policy(const Command& c);
    boardcore::Ack do_payout(const Command& c);
    boardcore::Ack do_vend_request(const Command& c);
    boardcore::Ack do_vend_result(const Command& c);
    boardcore::Ack do_end_session(const Command& c);
    boardcore::Ack do_return_escrow(const Command& c);
    boardcore::Ack do_device_reset(const Command& c);
    boardcore::Ack do_cash_sale(const Command& c);

    Link& link_;
    Clock& clock_;
    EventSink& sink_;
    Changer& changer_;
    Validator& validator_;
    Cashless& cashless_;
    Accounts& accounts_;
    EdgeWatch& edge_;
    const EventLog* log_ = nullptr;

    Policy policy_{};
    bool has_policy_ = false;
    bool silent_ = false;
    bool log_pressure_ = false;
    bool silent_end_pending_ = false;
    bool idle_wanted_ = true;
    bool lever_event_pending_ = false;
    Inhibit inhibit_ = Inhibit::NoPolicy;
    bool dirty_ = true;
    uint32_t signature_ = 0;

    bool wall_known_ = false;
    int64_t wall_unix_ = 0;
    uint32_t wall_set_ms_ = 0;

    // Payout bookkeeping.
    char active_payout_[65] = {};
    bool result_seen_ = false;
    PaymentEvent last_result_{};
    bool replay_pending_ = false;
    PaymentEvent replay_{};

    // Vend bookkeeping.
    char vend_req_id_[65] = {};
    bool denied_seen_ = false;
    char denied_detail_[64] = {};
    uint16_t request_item_ = 0;
    Approved approved_{};
    char session_ts_[25] = {};
};

}  // namespace mdbcore
