#include "master.h"

#include <stdio.h>
#include <string.h>

#include "boardcore/json_writer.h"
#include "wire.h"

namespace mdbcore {

using boardcore::Ack;
using boardcore::AckStatus;
using boardcore::JsonWriter;
using boardcore::Phase;

namespace {

void copy_str(char* dst, size_t cap, const char* src) {
    size_t i = 0;
    for (; src && src[i] && i + 1 < cap; ++i) dst[i] = src[i];
    dst[i] = '\0';
}

const char* kind_name(const Command& c) {
    if (c.name[0] != '\0') return c.name;
    const unsigned k = static_cast<unsigned>(c.kind);
    return k < 11 ? kCommands[k] : "unknown";
}

Ack make_ack(const Command& c, AckStatus status, const char* detail, Phase phase = Phase::Completed) {
    Ack a = {};
    copy_str(a.request_id, sizeof(a.request_id), c.request_id);
    copy_str(a.command, sizeof(a.command), kind_name(c));
    a.status = status;
    a.phase = phase;
    copy_str(a.detail, sizeof(a.detail), detail);
    return a;
}

// MDB item numbers are 16 bit; the contract's item is a 1-64 char string. A decimal string maps
// to its number, anything else to 0xFFFF (the MDB "unknown item").
uint16_t item_number(const char* s) {
    if (s == nullptr || *s == '\0') return 0xFFFF;
    uint32_t v = 0;
    for (; *s; ++s) {
        if (*s < '0' || *s > '9') return 0xFFFF;
        v = v * 10 + static_cast<uint32_t>(*s - '0');
        if (v > 0xFFFF) return 0xFFFF;
    }
    return static_cast<uint16_t>(v);
}

// "YYYY-MM-DDTHH:MM:SSZ" from unix seconds (civil-from-days, proleptic Gregorian).
void iso_from_unix(uint64_t t, char* out) {
    const int64_t days = static_cast<int64_t>(t / 86400);
    const uint32_t sod = static_cast<uint32_t>(t % 86400);
    const int64_t z = days + 719468;
    const int64_t era = z / 146097;
    const int64_t doe = z - era * 146097;
    const int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const int64_t mp = (5 * doy + 2) / 153;
    const int d = static_cast<int>(doy - (153 * mp + 2) / 5 + 1);
    const int m = static_cast<int>(mp < 10 ? mp + 3 : mp - 9);
    const int y = static_cast<int>(yoe + era * 400 + (m <= 2 ? 1 : 0));
    const int hh = static_cast<int>(sod / 3600), mm = static_cast<int>(sod % 3600 / 60),
              ss = static_cast<int>(sod % 60);
    out[0] = static_cast<char>('0' + y / 1000 % 10);
    out[1] = static_cast<char>('0' + y / 100 % 10);
    out[2] = static_cast<char>('0' + y / 10 % 10);
    out[3] = static_cast<char>('0' + y % 10);
    out[4] = '-';
    out[5] = static_cast<char>('0' + m / 10);
    out[6] = static_cast<char>('0' + m % 10);
    out[7] = '-';
    out[8] = static_cast<char>('0' + d / 10);
    out[9] = static_cast<char>('0' + d % 10);
    out[10] = 'T';
    out[11] = static_cast<char>('0' + hh / 10);
    out[12] = static_cast<char>('0' + hh % 10);
    out[13] = ':';
    out[14] = static_cast<char>('0' + mm / 10);
    out[15] = static_cast<char>('0' + mm % 10);
    out[16] = ':';
    out[17] = static_cast<char>('0' + ss / 10);
    out[18] = static_cast<char>('0' + ss % 10);
    out[19] = 'Z';
    out[20] = '\0';
}

}  // namespace

bool MasterTap::emit(const PaymentEvent& e) { return master_ != nullptr && master_->emit(e); }

EscrowVerdict MasterTap::judge(uint32_t bill_cents) {
    if (master_ == nullptr) return {false, BillReturnReason::Policy};
    return master_->judge(bill_cents);
}

// `ids` is part of the brief's signature but unused: the Cashless driver owns the session-id source.
Master::Master(Link& link, Clock& clock, IdSource& /*ids*/, EventSink& sink, Changer& changer,
               Validator& validator, Cashless& cashless, Accounts& accounts, EdgeWatch& edge)
    : link_(link),
      clock_(clock),
      sink_(sink),
      changer_(changer),
      validator_(validator),
      cashless_(cashless),
      accounts_(accounts),
      edge_(edge) {
    // No policy, no money: everything starts disabled, then the devices run their init ladders.
    // Always-idle is negotiated during the reader's first init ladder, which runs before any
    // policy exists, so it is wanted from boot; a disabled reader refuses vend requests anyway.
    cashless_.set_always_idle_wanted(true);
    apply_enables();
    changer_.reset();
    validator_.reset();
    cashless_.reset();
}

void Master::set_wall_time(int64_t unix_now) {
    wall_known_ = true;
    wall_unix_ = unix_now;
    wall_set_ms_ = clock_.now_ms();
    dirty_ = true;
}

uint64_t Master::now_unix() const {
    if (!wall_known_) return clock_.now_ms() / 1000;  // boot clock, best effort (display only)
    const int64_t t = wall_unix_ + static_cast<int64_t>((clock_.now_ms() - wall_set_ms_) / 1000);
    return t < 0 ? 0 : static_cast<uint64_t>(t);
}

bool Master::expired(const Command& c) const {
    if (c.timestamp_unix < 0 || !wall_known_) return false;
    return static_cast<int64_t>(now_unix()) - c.timestamp_unix > static_cast<int64_t>(timeout_s());
}

void Master::recompute_inhibit() {
    if (!has_policy_)
        inhibit_ = Inhibit::NoPolicy;
    else if (silent_)
        inhibit_ = Inhibit::EdgeSilent;
    else if (!policy_.accept)
        inhibit_ = Inhibit::PolicyOff;
    else
        inhibit_ = Inhibit::None;
    dirty_ = true;
}

// Pushes the enable state to the three devices; the bus transactions happen in their step().
void Master::apply_enables() {
    const bool on = accepting();
    static const uint32_t none[1] = {0};
    changer_.set_enabled(has_policy_ ? policy_.coin_enabled : none, has_policy_ ? policy_.n_coin : 0, on);
    validator_.set_enabled(has_policy_ ? policy_.bill_enabled : none, has_policy_ ? policy_.n_bill : 0, on);
    cashless_.set_enabled(on && policy_.cashless_enabled);
}

EscrowVerdict Master::judge(uint32_t bill_cents) {
    if (!accepting()) return {false, silent_ ? BillReturnReason::EdgeSilent : BillReturnReason::Policy};
    if (accounts_.would_exceed(bill_cents, policy_.max_escrow_cents)) return {false, BillReturnReason::Policy};
    if (changer_.exact_change(policy_.change_threshold_cents) && bill_cents > policy_.change_threshold_cents)
        return {false, BillReturnReason::Policy};
    return {true, BillReturnReason::Policy};
}

bool Master::emit(const PaymentEvent& e) {
    // The outer sink first: a driver re-emits a refused event, so booking before it accepted
    // would count the same coin or stack twice.
    if (!sink_.emit(e)) return false;
    apply(e);
    return true;
}

void Master::apply(const PaymentEvent& e) {
    switch (e.kind) {
        case EventKind::CoinAccepted:
            accounts_.coin(e.cents);
            break;
        case EventKind::BillEscrowed:
            accounts_.bill_escrowed(e.cents);
            break;
        case EventKind::BillStacked:
            accounts_.bill_stacked(e.cents);
            accounts_.bill_escrow_cleared();
            break;
        case EventKind::BillReturned:
            accounts_.bill_escrow_cleared();
            break;
        case EventKind::PayoutResult:
            // Coins that left the tubes are no longer held; only a first-seen result deducts.
            if (accounts_.remember_payout(e.request_id, e.payout_status, e.requested_cents, e.paid_cents) &&
                e.paid_cents > 0)
                accounts_.cash_sale(e.paid_cents);
            if (strcmp(e.request_id, active_payout_) == 0) {
                result_seen_ = true;
                last_result_ = e;
                active_payout_[0] = '\0';
            }
            break;
        case EventKind::SessionBegun:
            iso_from_unix(now_unix(), session_ts_);
            break;
        case EventKind::VendApproved:
            if (strcmp(e.request_id, vend_req_id_) == 0) {
                approved_.active = true;
                copy_str(approved_.session_id, sizeof(approved_.session_id), e.session_id);
                copy_str(approved_.request_id, sizeof(approved_.request_id), e.request_id);
                approved_.item = request_item_;
            }
            break;
        case EventKind::VendDenied:
            if (strcmp(e.request_id, vend_req_id_) == 0) {
                denied_seen_ = true;
                copy_str(denied_detail_, sizeof(denied_detail_), e.detail);
            }
            break;
        case EventKind::SessionEnded:
            approved_.active = false;
            break;
        default:
            break;
    }
    dirty_ = true;
}

void Master::after_changer_step() {
    // The changer only latches the lever; the event is the Master's. The flag is always consumed,
    // only a policy that asks for it returns the bill, and the event is kept until the sink took it.
    if (changer_.escrow_lever_pending()) {
        lever_event_pending_ = true;
        if (has_policy_ && policy_.return_bill_on_lever) validator_.return_escrow(BillReturnReason::Customer);
    }
    if (lever_event_pending_ && emit(make_event(EventKind::EscrowReturnPressed, Device::Coin)))
        lever_event_pending_ = false;
}

void Master::step_changer() {
    changer_.step();
    after_changer_step();
}
void Master::step_validator() { validator_.step(); }
void Master::step_cashless() { cashless_.step(); }

void Master::on_silence() {
    silent_ = true;
    recompute_inhibit();
    apply_enables();
    uint32_t cents = 0;
    if (validator_.bill_in_escrow(cents)) validator_.return_escrow(BillReturnReason::EdgeSilent);
    silent_end_pending_ = true;
}

uint32_t Master::state_signature() const {
    uint32_t h = 2166136261u;
    auto mix = [&h](uint32_t v) { h = (h ^ v) * 16777619u; };
    uint8_t n = 0;
    const TubeLevel* t = changer_.tubes(n);
    for (uint8_t i = 0; i < n; ++i) {
        mix(t[i].count);
        mix(t[i].full);
    }
    uint16_t sc = 0;
    bool sf = false;
    mix(validator_.stacker(sc, sf));
    mix(sc);
    mix(sf);
    mix(changer_.exact_change(has_policy_ ? policy_.change_threshold_cents : 0));
    mix(accounts_.escrow_cents());
    return h;
}

void Master::step() {
    if (edge_.take_silence_edge()) on_silence();
    if (silent_end_pending_) {
        char sid[17];
        bool fk = false;
        uint32_t fc = 0;
        if (!cashless_.session_open(sid, fk, fc) || cashless_.end_session()) silent_end_pending_ = false;
    }
    if (replay_pending_ && sink_.emit(replay_)) replay_pending_ = false;
    // A device whose last money event the sink refused skips its own transaction (flush first).
    step_changer();
    step_validator();
    step_cashless();
    const uint32_t sig = state_signature();
    if (sig != signature_) {
        signature_ = sig;
        dirty_ = true;
    }
}

bool Master::poll_completion(Ack& /*out*/) { return false; }

Ack Master::submit(const Command& c) {
    edge_.seen();
    switch (c.kind) {
        case CmdKind::Ping:
            return make_ack(c, AckStatus::Ok, "");
        case CmdKind::SelfTest: {
            Ack a = make_ack(c, AckStatus::Ok, "");
            Ack t = {};
            self_test(t);
            copy_str(a.result_json, sizeof(a.result_json), t.result_json);
            return a;
        }
        case CmdKind::ForceReport:
            force_report();
            return make_ack(c, AckStatus::Ok, "");
        case CmdKind::SetPolicy:
            return do_set_policy(c);
        case CmdKind::Payout:
            return do_payout(c);
        case CmdKind::VendRequest:
            return do_vend_request(c);
        case CmdKind::VendResult:
            return do_vend_result(c);
        case CmdKind::EndSession:
            return do_end_session(c);
        case CmdKind::ReturnEscrow:
            return do_return_escrow(c);
        case CmdKind::DeviceReset:
            return do_device_reset(c);
        case CmdKind::CashSale:
            return do_cash_sale(c);
        case CmdKind::Unknown:
            break;
    }
    return make_ack(c, AckStatus::Unsupported, "unsupported command");
}

Ack Master::do_set_policy(const Command& c) {
    const Policy& p = c.policy;
    if (p.policy_id[0] == '\0') return make_ack(c, AckStatus::Rejected, "policy_id");
    if (p.edge_timeout_s < 5 || p.edge_timeout_s > 120) return make_ack(c, AckStatus::Rejected, "edge_timeout_s");
    if (p.max_escrow_cents == 0) return make_ack(c, AckStatus::Rejected, "max_escrow_cents");
    if (p.n_coin > 16 || p.n_bill > 16) return make_ack(c, AckStatus::Rejected, "enabled list too long");

    // Nothing below is final until the ack is known: a failed set_policy puts all of it back, so it
    // never lifts edge silence or resumes acceptance (rule 1, and the fail-safe section).
    const Policy prev_policy = policy_;
    const bool prev_has = has_policy_, prev_silent = silent_, prev_silent_end = silent_end_pending_;
    policy_ = p;
    has_policy_ = true;
    silent_ = false;  // only an acked set_policy lifts edge silence
    silent_end_pending_ = false;
    edge_.set_timeout_ms(static_cast<uint32_t>(p.edge_timeout_s) * 1000);
    cashless_.set_edge_timeout_ms(static_cast<uint32_t>(p.edge_timeout_s) * 1000);
    // Always-idle is only negotiated during the reader's init ladder. It is wanted from boot, so
    // the normal case needs no reset; turning cashless back on after a policy that did not want
    // it restarts the ladder (an open session ends). A disabled reader refuses vend requests.
    cashless_.set_always_idle_wanted(p.cashless_enabled);
    const bool want_reset =
        p.cashless_enabled && !idle_wanted_ && cashless_.state().present && !cashless_.always_idle();
    idle_wanted_ = p.cashless_enabled;
    if (want_reset) cashless_.reset();
    recompute_inhibit();
    apply_enables();

    // Drive each present, polling device until it ACKed the enable update. A device that is
    // absent or still initialising picks the enables up when it comes up, so it is not waited for.
    auto changer_p = [&] {
        return changer_.state().present && changer_.phase() == Changer::Phase::Polling && changer_.enable_pending();
    };
    auto validator_p = [&] {
        return validator_.state().present && validator_.phase() == Validator::Phase::Polling &&
               validator_.enable_pending();
    };
    auto cashless_p = [&] {
        return cashless_.state().present && cashless_.phase() == Cashless::Phase::Polling &&
               cashless_.enable_pending();
    };
    for (int r = 0; r < kSettleRounds; ++r) {
        bool any = false;
        if (changer_p()) {
            step_changer();
            any = true;
        }
        if (validator_p()) {
            step_validator();
            any = true;
        }
        if (cashless_p()) {
            step_cashless();
            any = true;
        }
        if (!any) break;
    }
    dirty_ = true;
    const char* failed = changer_p() ? "enable_pending:coin"
                         : validator_p() ? "enable_pending:bill"
                         : cashless_p() ? "enable_pending:cashless"
                                        : nullptr;
    if (failed == nullptr) return make_ack(c, AckStatus::Ok, "");
    policy_ = prev_policy;
    has_policy_ = prev_has;
    silent_ = prev_silent;
    silent_end_pending_ = prev_silent_end;
    const uint32_t ms = prev_has ? static_cast<uint32_t>(prev_policy.edge_timeout_s) * 1000 : 30000;
    edge_.set_timeout_ms(ms);
    cashless_.set_edge_timeout_ms(ms);
    idle_wanted_ = prev_has ? prev_policy.cashless_enabled : true;
    cashless_.set_always_idle_wanted(idle_wanted_);
    recompute_inhibit();
    apply_enables();
    return make_ack(c, AckStatus::Failed, failed);
}

// Payout is neither gated on the policy nor on an inhibit: coins already taken are owed, and
// after edge silence the edge pays them out before it re-sends set_policy.
Ack Master::do_payout(const Command& c) {
    if (expired(c)) return make_ack(c, AckStatus::Rejected, "expired");
    if (c.cents == 0) return make_ack(c, AckStatus::Rejected, "cents");

    PaymentEvent stored;
    if (accounts_.find_payout(c.request_id, stored)) {
        // Same request_id: replay the stored result, never pay twice.
        replay_ = stored;
        replay_pending_ = true;
        if (sink_.emit(replay_)) replay_pending_ = false;
        return make_ack(c, AckStatus::Ok, "replay", Phase::Accepted);
    }
    // Retry of a payout the driver still holds: queued, running, or ended with its PayoutResult
    // held back by a full sink. Never `failed` (rule 5: failed is final); the result event decides.
    if (strcmp(active_payout_, c.request_id) == 0) return make_ack(c, AckStatus::Ok, "in_progress", Phase::Accepted);
    if (changer_.payout_in_progress()) return make_ack(c, AckStatus::Rejected, "busy");

    result_seen_ = false;
    copy_str(active_payout_, sizeof(active_payout_), c.request_id);
    if (!changer_.request_payout(c.cents, c.request_id)) {
        // Nothing is in progress (checked above), so this one cannot be expressed or the changer is away.
        active_payout_[0] = '\0';
        return make_ack(c, AckStatus::Rejected, changer_.state().present ? "unpayable" : "changer_offline");
    }
    // request_payout() only queues the payout. Run the changer until it ACKed the PAYOUT /
    // DISPENSE command (or ended the payout itself), and answer from that.
    for (int r = 0; r < kSettleRounds; ++r) {
        if (result_seen_ || changer_.payout_acked() || !changer_.payout_in_progress()) break;
        step_changer();
    }
    if (result_seen_) {
        if (last_result_.payout_status == PayoutStatus::Failed)
            return make_ack(c, AckStatus::Failed, last_result_.detail);
        return make_ack(c, AckStatus::Ok, "", Phase::Accepted);
    }
    // Acked on the bus, or still queued behind a slow changer, or ended with the result held back by
    // a full sink: in every case the driver holds the command and active_payout_ still names it, so
    // the answer is `accepted`; a terminal failure only ever arrives as the payout_result event.
    return make_ack(c, AckStatus::Ok, "", Phase::Accepted);
}

Ack Master::do_vend_request(const Command& c) {
    if (expired(c)) return make_ack(c, AckStatus::Rejected, "expired");
    if (c.cents == 0) return make_ack(c, AckStatus::Rejected, "cents");
    char sid[17];
    bool funds_known = false;
    uint32_t funds = 0;
    const bool open = cashless_.session_open(sid, funds_known, funds);
    if (c.has_session_id) {
        if (!open) return make_ack(c, AckStatus::Rejected, "no_session");
        if (strcmp(sid, c.session_id) != 0) return make_ack(c, AckStatus::Rejected, "session_mismatch");
    } else if (!cashless_.always_idle()) {
        return make_ack(c, AckStatus::Rejected, "no_session");  // price-first: the edge waits for a tap
    }

    denied_seen_ = false;
    denied_detail_[0] = '\0';
    copy_str(vend_req_id_, sizeof(vend_req_id_), c.request_id);
    request_item_ = item_number(c.item);
    if (!cashless_.vend_request(c.cents, request_item_, c.request_id)) {
        vend_req_id_[0] = '\0';
        return make_ack(c, AckStatus::Rejected, "busy");
    }
    // Like the payout: queued only. Accept once the reader ACKed VEND REQUEST on the bus.
    for (int r = 0; r < kSettleRounds; ++r) {
        if (denied_seen_ || cashless_.vend_command_sent()) break;
        step_cashless();
    }
    if (denied_seen_) return make_ack(c, AckStatus::Rejected, denied_detail_);
    // Acked, or still queued behind a slow reader: the driver holds the request either way, so it is
    // accepted. A later denial arrives as the vend_denied event, never as a failed ack.
    return make_ack(c, AckStatus::Ok, "", Phase::Accepted);
}

// An immediate command is acknowledged `ok` once the bus confirmed it. A NAK is a failed ack; if the
// settle rounds run out with the action still queued it will still run, so the ack says "queued".
Ack Master::settle_cashless_action(const Command& c) {
    for (int r = 0; r < kSettleRounds; ++r) {
        if (!cashless_.action_pending() || cashless_.action_nak()) break;
        step_cashless();
    }
    dirty_ = true;
    if (cashless_.action_nak()) return make_ack(c, AckStatus::Failed, "nak");
    return make_ack(c, AckStatus::Ok, cashless_.action_pending() ? "queued" : "");
}

Ack Master::do_vend_result(const Command& c) {
    if (!approved_.active) return make_ack(c, AckStatus::Rejected, "no_vend");
    if (strcmp(c.session_id, approved_.session_id) != 0) return make_ack(c, AckStatus::Rejected, "session_mismatch");
    if (strcmp(c.vend_request_id, approved_.request_id) != 0)
        return make_ack(c, AckStatus::Rejected, "request_mismatch");
    if (!cashless_.vend_result(c.success, approved_.item)) return make_ack(c, AckStatus::Rejected, "no_vend");
    approved_.active = false;
    dirty_ = true;
    return settle_cashless_action(c);
}

Ack Master::do_end_session(const Command& c) {
    char sid[17];
    bool fk = false;
    uint32_t fc = 0;
    if (!cashless_.session_open(sid, fk, fc)) return make_ack(c, AckStatus::Rejected, "no_session");
    if (strcmp(sid, c.session_id) != 0) return make_ack(c, AckStatus::Rejected, "session_mismatch");
    if (!cashless_.end_session()) return make_ack(c, AckStatus::Rejected, "busy");
    return settle_cashless_action(c);
}

Ack Master::do_return_escrow(const Command& c) {
    uint32_t cents = 0;
    if (!validator_.bill_in_escrow(cents)) return make_ack(c, AckStatus::Ok, "no_bill");
    if (!validator_.return_escrow(BillReturnReason::Edge)) return make_ack(c, AckStatus::Rejected, "busy");
    for (int r = 0; r < kSettleRounds; ++r) {
        if (!validator_.escrow_command_pending() || validator_.escrow_nak()) break;
        step_validator();
    }
    dirty_ = true;
    if (validator_.escrow_nak()) return make_ack(c, AckStatus::Failed, "nak");
    return make_ack(c, AckStatus::Ok, validator_.escrow_command_pending() ? "queued" : "");
}

Ack Master::do_device_reset(const Command& c) {
    switch (c.device) {
        case Device::Coin:
            changer_.reset();
            break;
        case Device::Bill:
            validator_.reset();
            break;
        case Device::Cashless:
            cashless_.reset();
            break;
    }
    dirty_ = true;
    return make_ack(c, AckStatus::Ok, "");
}

// Not gated on the policy either: a sale that already happened is booked whatever the inhibit.
Ack Master::do_cash_sale(const Command& c) {
    if (c.cents == 0) return make_ack(c, AckStatus::Rejected, "cents");
    const bool exact = accounts_.cash_sale(c.cents);
    if (cashless_.state().present) cashless_.cash_sale(c.cents, 0);  // a notification; a refusal is not an error
    dirty_ = true;
    return make_ack(c, AckStatus::Ok, exact ? "" : "clamped");
}

void Master::snapshot(PaymentState& s) const {
    s.policy_id = has_policy_ ? policy_.policy_id : nullptr;
    s.accepting = inhibit_ == Inhibit::None;
    s.inhibited = inhibit_;
    s.escrow_cents = accounts_.escrow_cents();
    uint32_t bill = 0;
    s.bill_in_escrow = accounts_.bill_in_escrow(bill);
    s.bill_in_escrow_cents = s.bill_in_escrow ? bill : 0;
    s.devices[0] = changer_.state();
    s.devices[1] = validator_.state();
    s.devices[2] = cashless_.state();
    uint8_t n = 0;
    const TubeLevel* t = changer_.tubes(n);
    s.n_tubes = n > 16 ? 16 : n;
    for (uint8_t i = 0; i < s.n_tubes; ++i) s.tubes[i] = t[i];
    s.exact_change = changer_.exact_change(has_policy_ ? policy_.change_threshold_cents : 0);
    s.stacker_known = validator_.stacker(s.stacker_count, s.stacker_full);
    if (!s.stacker_known) {
        s.stacker_count = 0;
        s.stacker_full = false;
    }
    char sid[17];
    bool fk = false;
    uint32_t fc = 0;
    s.session_open = cashless_.session_open(sid, fk, fc);
    s.session_id[0] = '\0';
    if (s.session_open) copy_str(s.session_id, sizeof(s.session_id), sid);
    s.funds_known = s.session_open && fk;
    s.funds_cents = s.funds_known ? fc : 0;
    s.session_started_ts = s.session_open ? session_ts_ : nullptr;
}

void Master::self_test(Ack& out) const {
    out = Ack{};
    copy_str(out.command, sizeof(out.command), kCommands[1]);
    out.status = AckStatus::Ok;
    out.phase = Phase::Completed;
    JsonWriter w(out.result_json, sizeof(out.result_json));
    w.raw("{\"checks\":[");
    auto check = [&w](bool first, const char* name, bool pass, const char* detail) {
        if (!first) w.ch(',');
        w.ch('{');
        w.field("name", name);
        w.raw(",\"pass\":");
        w.boolean(pass);
        w.ch(',');
        w.field("detail", detail);
        w.ch('}');
    };
    auto device = [&](bool first, const char* name, const DeviceState& d) {
        char detail[32];
        if (!d.present)
            copy_str(detail, sizeof(detail), "absent");
        else
            snprintf(detail, sizeof(detail), "level %d, %s", d.level, status_str(d.status));
        const bool ok = d.present && d.status != Status::Offline && d.status != Status::Jammed &&
                        d.status != Status::Error && d.status != Status::Tilt;
        check(first, name, ok, detail);
    };
    device(true, "changer", changer_.state());
    device(false, "validator", validator_.state());
    device(false, "reader", cashless_.state());
    check(false, "clock_synced", wall_known_, wall_known_ ? "synced" : "not synced");
    char buf[32];
    if (log_ != nullptr) {
        snprintf(buf, sizeof(buf), "depth %d", log_->pending());
        check(false, "event_log", log_->pending() < EventLog::kCapacity * 3 / 4, buf);
    } else {
        check(false, "event_log", true, "not attached");
    }
    snprintf(buf, sizeof(buf), "%u disagreements", static_cast<unsigned>(link_.mode_disagreements()));
    check(false, "rx_mode_bit", link_.mode_disagreements() == 0, buf);
    if (has_policy_)
        snprintf(buf, sizeof(buf), "%.20s", policy_.policy_id);
    else
        snprintf(buf, sizeof(buf), "none");
    check(false, "policy", has_policy_, buf);
    const uint32_t dropped = changer_.dropped_events() + validator_.dropped_events() + cashless_.dropped_events();
    snprintf(buf, sizeof(buf), "%u dropped", static_cast<unsigned>(dropped));
    check(false, "event_queue", dropped == 0, buf);
    w.raw("]}");
    if (w.done() == 0) out.result_json[0] = '\0';
}

}  // namespace mdbcore
