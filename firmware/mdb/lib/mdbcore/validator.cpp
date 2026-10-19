#include "validator.h"
#include "text.h"

#include <string.h>

namespace mdbcore {

namespace {

constexpr uint8_t kCmdReset = 0, kCmdSetup = 1, kCmdPoll = 3, kCmdBillType = 4, kCmdEscrow = 5,
                  kCmdStacker = 6, kCmdExpansion = 7;
constexpr uint8_t kSubIdent = 0x00;
constexpr uint8_t kSetupLen = 27, kIdentMinLen = 29;

void copy_str(char* dst, size_t cap, const char* src) {
    size_t i = 0;
    for (; src && src[i] && i + 1 < cap; ++i) dst[i] = src[i];
    dst[i] = '\0';
}

int status_rank(Status s) {
    return s == Status::Offline ? 4 : s == Status::Jammed ? 3 : s == Status::Error ? 2 : 1;
}

}  // namespace

Validator::Validator(Link& link, Clock& clock, EventSink& sink, EscrowJudge& judge)
    : link_(link), clock_(clock), sink_(sink), judge_(judge) {
    state_.present = false;
    state_.was_present = false;
    state_.status = Status::Offline;
}

void Validator::push(const PaymentEvent& e) {
    // A POLL block is at most 16 bytes and step() never parses a new block while pending_ is
    // non-empty, so 16 suffices. Overflow is counted, never silent.
    if (n_pending_ < kPending)
        pending_[n_pending_++] = e;
    else
        ++dropped_events_;
}

bool Validator::flush() {
    uint8_t sent = 0;
    while (sent < n_pending_ && sink_.emit(pending_[sent])) ++sent;
    if (sent > 0) {
        for (uint8_t i = sent; i < n_pending_; ++i) pending_[i - sent] = pending_[i];
        n_pending_ = static_cast<uint8_t>(n_pending_ - sent);
    }
    return n_pending_ == 0;
}

void Validator::bill_event(EventKind kind, uint32_t cents, BillReturnReason reason) {
    PaymentEvent e = make_event(kind, Device::Bill);
    e.cents = cents;
    e.bill_reason = reason;
    push(e);
}

void Validator::update_status(Status s, int16_t mdb, const char* detail, bool force) {
    if (s == state_.status && !force) return;
    state_.status = s;
    PaymentEvent e = make_event(EventKind::DeviceStatus, Device::Bill);
    e.status = s;
    e.mdb_status = mdb;
    copy_str(e.detail, sizeof(e.detail), detail);
    push(e);
}

Status Validator::base_status() const {
    if (stacker_full_) return Status::Full;
    if (state_.n_enabled == 0) return Status::Disabled;
    return Status::Ready;
}

uint16_t Validator::enable_mask() const {
    uint16_t m = 0;
    for (uint8_t t = 0; t < 16; ++t) {
        if (!type_valid(t)) continue;
        for (uint8_t j = 0; j < state_.n_enabled; ++j)
            if (state_.enabled[j] == type_cents(t)) m = static_cast<uint16_t>(m | (1u << t));
    }
    return m;
}

void Validator::refilter() {
    state_.n_enabled = 0;
    if (!accept_) return;
    for (uint8_t i = 0; i < n_want_; ++i) {
        bool known = false, dup = false;
        for (uint8_t d = 0; d < state_.n_denominations; ++d) known = known || state_.denominations[d] == want_[i];
        for (uint8_t e = 0; e < state_.n_enabled; ++e) dup = dup || state_.enabled[e] == want_[i];
        if (known && !dup) state_.enabled[state_.n_enabled++] = want_[i];
    }
}

void Validator::set_enabled(const uint32_t* cents, uint8_t n, bool accept) {
    accept_ = accept;
    n_want_ = 0;
    for (uint8_t i = 0; i < n && i < 16; ++i) want_[n_want_++] = cents[i];
    refilter();
    mask_dirty_ = true;
}

void Validator::clear_escrow() {
    held_ = false;
    held_cents_ = 0;
    escrow_cmd_ = EscrowCmd::None;
    escrow_sent_ = false;
    return_requested_ = false;
}

// The validator lost the bill's context (reset or offline): tell the Master it was not stacked.
void Validator::lose_escrow() {
    if (held_) bill_event(EventKind::BillReturned, held_cents_, BillReturnReason::Timeout);
    clear_escrow();
}

void Validator::reset() {
    lose_escrow();
    phase_ = Phase::Reset;
    mask_dirty_ = true;
    swallow_reset_ = false;
}

bool Validator::return_escrow(BillReturnReason reason) {
    if (!held_ || escrow_sent_) return false;
    escrow_cmd_ = EscrowCmd::Return;
    escrow_nak_ = false;
    return_reason_ = reason;
    return true;
}

void Validator::note_miss() {
    if (offline_reported_ || link_.misses(kAddr) < kMissThreshold) return;
    offline_reported_ = true;
    state_.present = false;
    lose_escrow();
    update_status(Status::Offline, -1, "no response");
    phase_ = Phase::Reset;
    mask_dirty_ = true;
    last_attempt_ms_ = clock_.now_ms();
}

void Validator::enter_polling() {
    phase_ = Phase::Polling;
    const bool announce = !escrow_capable_ && !no_escrow_reported_;
    update_status(base_status(), -1, escrow_capable_ ? "" : "no_escrow", announce);
    no_escrow_reported_ = true;
}

void Validator::step() {
    if (!flush()) return;  // never start a new transaction past an undelivered event
    switch (phase_) {
        case Phase::Inactive:
            return;
        case Phase::Reset:
            if (offline_reported_ && clock_.now_ms() - last_attempt_ms_ < kOfflineRetryMs) return;
            last_attempt_ms_ = clock_.now_ms();
            do_reset();
            break;
        case Phase::Setup:
            do_setup();
            break;
        case Phase::Identify:
            do_identify();
            break;
        case Phase::Stacker:
            do_stacker();
            break;
        case Phase::BillType:
            do_bill_type();
            break;
        case Phase::Polling:
            if (mask_dirty_) {
                do_bill_type();
            } else if (escrow_cmd_ != EscrowCmd::None) {
                do_escrow();
            } else if (stacker_due_ || clock_.now_ms() - last_stacker_ms_ >= kStackerRefreshMs) {
                do_stacker();
            } else {
                do_poll();
            }
            break;
    }
    flush();
}

void Validator::do_reset() {
    Response r = link_.transact(kAddr, kCmdReset, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    offline_reported_ = false;
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Nak) return;
    swallow_reset_ = true;  // the validator announces its own reset (0x06) on the first POLL
    phase_ = Phase::Setup;
}

void Validator::do_setup() {
    Response r = link_.transact(kAddr, kCmdSetup, nullptr, 0);
    if (r.kind != Reply::Data) {
        if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) note_miss();
        return;
    }
    link_.acknowledge();
    offline_reported_ = false;
    if (r.len < kSetupLen) return;
    const uint8_t level = r.data[0];
    scaling_ = static_cast<uint16_t>((r.data[3] << 8) | r.data[4]);
    escrow_capable_ = r.data[10] == 0xFF;
    state_.n_denominations = 0;
    for (uint8_t t = 0; t < 16; ++t) {
        credit_[t] = r.data[11 + t];
        if (type_valid(t)) state_.denominations[state_.n_denominations++] = type_cents(t);
    }
    refilter();  // a policy received before SETUP now has denominations to be checked against
    state_.level = static_cast<int8_t>(level >= 2 ? 2 : level);
    state_.present = true;
    state_.was_present = true;
    no_escrow_reported_ = false;
    phase_ = level >= 2 ? Phase::Identify : Phase::Stacker;
}

void Validator::do_identify() {
    const uint8_t sub = kSubIdent;
    Response r = link_.transact(kAddr, kCmdExpansion, &sub, 1);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) {
        link_.acknowledge();
        if (r.len >= kIdentMinLen) {
            copy_field(state_.serial, sizeof(state_.serial), r.data + 3, 12);
            copy_field(state_.model, sizeof(state_.model), r.data + 15, 12);
        }
    }
    phase_ = Phase::Stacker;  // NAK: carry on without the optional identification
}

void Validator::do_stacker() {
    Response r = link_.transact(kAddr, kCmdStacker, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    last_stacker_ms_ = clock_.now_ms();
    stacker_due_ = false;
    if (r.kind == Reply::Data && r.len >= 2) {
        const uint16_t v = static_cast<uint16_t>((r.data[0] << 8) | r.data[1]);
        stacker_full_ = (v & 0x8000) != 0;
        stacker_count_ = v & 0x7FFF;
        stacker_known_ = true;
        if (phase_ == Phase::Polling && !cashbox_out_) update_status(base_status(), -1, "");
    }
    if (phase_ == Phase::Stacker) phase_ = Phase::BillType;
}

void Validator::do_bill_type() {
    const uint16_t m = accept_ ? enable_mask() : 0;
    const uint16_t e = escrow_capable_ ? m : 0;
    const uint8_t d[4] = {static_cast<uint8_t>(m >> 8), static_cast<uint8_t>(m & 0xFF),
                          static_cast<uint8_t>(e >> 8), static_cast<uint8_t>(e & 0xFF)};
    Response r = link_.transact(kAddr, kCmdBillType, d, 4);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind != Reply::Ack) return;  // keep dirty, retry
    mask_dirty_ = false;
    if (phase_ == Phase::BillType) enter_polling();
}

void Validator::do_escrow() {
    if (escrow_cmd_ == EscrowCmd::Judge) {
        const EscrowVerdict v = judge_.judge(held_cents_);
        escrow_cmd_ = v.stack ? EscrowCmd::Stack : EscrowCmd::Return;
        return_reason_ = v.reason;
    }
    const bool stack = escrow_cmd_ == EscrowCmd::Stack;
    const uint8_t d = stack ? 0x01 : 0x00;
    Response r = link_.transact(kAddr, kCmdEscrow, &d, 1);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();  // the decided command stays queued and is retried
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    escrow_nak_ = r.kind == Reply::Nak;
    if (r.kind == Reply::Ack) {
        escrow_sent_ = true;
        return_requested_ = !stack;
    }
    // On ACK the follow-up poll reports the outcome; on NAK we give up and the validator's own
    // escrow timeout resolves the bill.
    escrow_cmd_ = EscrowCmd::None;
}

void Validator::do_poll() {
    Response r = link_.transact(kAddr, kCmdPoll, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    offline_reported_ = false;
    if (r.kind == Reply::Nak) return;
    const bool swallow = swallow_reset_;
    swallow_reset_ = false;
    const uint8_t len = r.kind == Reply::Data ? r.len : 0;

    bool have = false, was_reset = false, saw_cashbox = false;
    Status worst = Status::Ready;
    int16_t worst_byte = -1;
    for (uint8_t i = 0; i < len; ++i) {
        const uint8_t b = r.data[i];
        if (b & 0x80) {
            const uint8_t type = b & 0x0F, kind = (b >> 4) & 0x07;
            const uint32_t cents = type_valid(type) ? type_cents(type) : 0;
            switch (kind) {
                case 0:  // stacked
                case 7:  // transferred to stacker
                    if (cents > 0) bill_event(EventKind::BillStacked, cents, BillReturnReason::Policy);
                    clear_escrow();
                    stacker_due_ = true;
                    break;
                case 1:  // escrow position
                    if (held_ || cents == 0) break;
                    held_ = true;
                    held_cents_ = cents;
                    escrow_cmd_ = EscrowCmd::Judge;
                    escrow_sent_ = false;
                    return_requested_ = false;
                    bill_event(EventKind::BillEscrowed, cents, BillReturnReason::Policy);
                    break;
                case 2: {  // returned
                    const BillReturnReason why = return_requested_ ? return_reason_ : BillReturnReason::Timeout;
                    if (cents > 0) bill_event(EventKind::BillReturned, cents, why);
                    clear_escrow();
                    break;
                }
                default:  // disabled-bill rejected (4) and recycler codes: nothing credited
                    break;
            }
        } else if ((b & 0xF0) == 0x20) {
            // attempts to insert while disabled: nothing to report
        } else if (b >= 0x01 && b <= 0x0C) {
            Status s = Status::Error;
            switch (b) {
                case 0x03:
                    s = Status::Busy;
                    break;
                case 0x05:
                    s = Status::Jammed;
                    break;
                case 0x06:
                    if (swallow) continue;
                    was_reset = true;
                    s = Status::Busy;
                    break;
                case 0x07:  // bill removed from escrow by hand
                    lose_escrow();
                    continue;
                case 0x08:
                    s = Status::Offline;
                    saw_cashbox = true;
                    cashbox_out_ = true;
                    break;
                case 0x09:
                    s = Status::Disabled;
                    break;
                case 0x0B:  // bill rejected: nothing credited
                    continue;
                default:
                    s = Status::Error;  // 01 02 04 0A 0C
                    break;
            }
            if (!have || status_rank(s) > status_rank(worst)) {
                worst = s;
                worst_byte = b;
            }
            have = true;
        }
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    if (cashbox_out_ && !saw_cashbox) {
        cashbox_out_ = false;
        push(make_event(EventKind::CashCollected, Device::Bill));
    }
    if (have) {
        const char* detail = was_reset ? "validator was reset" : worst_byte == 0x08 ? "cash box out" : "";
        update_status(worst, worst_byte, detail);
    } else {
        update_status(base_status(), -1, "");
    }
    if (was_reset) {
        lose_escrow();
        mask_dirty_ = true;
        phase_ = Phase::Setup;
    }
}

}  // namespace mdbcore
