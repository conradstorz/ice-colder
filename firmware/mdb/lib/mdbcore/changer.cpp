#include "changer.h"

#include <string.h>

namespace mdbcore {

namespace {

constexpr uint8_t kCmdReset = 0, kCmdSetup = 1, kCmdTube = 2, kCmdPoll = 3, kCmdCoinType = 4,
                  kCmdDispense = 5, kCmdExpansion = 7;
constexpr uint8_t kSubIdent = 0x00, kSubFeature = 0x01, kSubPayout = 0x02, kSubPayoutStatus = 0x03,
                  kSubPayoutValue = 0x04;
constexpr uint8_t kSetupLen = 23, kTubeLen = 18, kIdentMinLen = 29;

void copy_str(char* dst, size_t cap, const char* src) {
    size_t i = 0;
    for (; src && src[i] && i + 1 < cap; ++i) dst[i] = src[i];
    dst[i] = '\0';
}

// Copies a space/NUL padded fixed-width field, trimming the padding.
void copy_field(char* dst, size_t cap, const uint8_t* src, size_t n) {
    size_t len = n < cap - 1 ? n : cap - 1;
    while (len > 0 && (src[len - 1] == ' ' || src[len - 1] == 0)) --len;
    memcpy(dst, src, len);
    dst[len] = '\0';
}

int status_rank(Status s) {
    return s == Status::Offline ? 4 : s == Status::Jammed ? 3 : s == Status::Error ? 2 : 1;
}

}  // namespace

Changer::Changer(Link& link, Clock& clock, EventSink& sink) : link_(link), clock_(clock), sink_(sink) {
    state_.present = false;
    state_.status = Status::Offline;
}

void Changer::push(const PaymentEvent& e) {
    // 16 is enough: a POLL block is at most 16 bytes, so it yields at most 16 events, and step()
    // never parses a new block while pending_ is non-empty. Other pushes (status, payout result)
    // are single events made between flushes. Overflow is counted, never silent.
    if (n_pending_ < kPending)
        pending_[n_pending_++] = e;
    else
        ++dropped_events_;
}

bool Changer::flush() {
    uint8_t sent = 0;
    while (sent < n_pending_ && sink_.emit(pending_[sent])) ++sent;
    if (sent > 0) {
        for (uint8_t i = sent; i < n_pending_; ++i) pending_[i - sent] = pending_[i];
        n_pending_ = static_cast<uint8_t>(n_pending_ - sent);
    }
    return n_pending_ == 0;
}

void Changer::update_status(Status s, int16_t mdb, const char* detail) {
    if (s == state_.status) return;
    state_.status = s;
    PaymentEvent e = make_event(EventKind::DeviceStatus, Device::Coin);
    e.status = s;
    e.mdb_status = mdb;
    copy_str(e.detail, sizeof(e.detail), detail);
    push(e);
}

Status Changer::base_status() const {
    if (state_.n_enabled == 0) return Status::Disabled;
    bool any_tubed = false, any_stock = false;
    for (uint8_t t = 0; t < 16; ++t) {
        if (!type_tubed(t)) continue;
        bool enabled = false;
        for (uint8_t j = 0; j < state_.n_enabled; ++j)
            if (state_.enabled[j] == type_cents(t)) enabled = true;
        if (!enabled) continue;
        any_tubed = true;
        if (counts_[t] > 0) any_stock = true;
    }
    return (any_tubed && !any_stock) ? Status::Empty : Status::Ready;
}

uint16_t Changer::enable_mask() const {
    uint16_t m = 0;
    for (uint8_t t = 0; t < 16; ++t) {
        if (!type_valid(t)) continue;
        for (uint8_t j = 0; j < state_.n_enabled; ++j)
            if (state_.enabled[j] == type_cents(t)) m = static_cast<uint16_t>(m | (1u << t));
    }
    return m;
}

void Changer::rebuild_tubes() {
    n_tubes_ = 0;
    for (uint8_t t = 0; t < 16; ++t) {
        if (!type_tubed(t)) continue;
        tubes_[n_tubes_] = TubeLevel{type_cents(t), counts_[t], static_cast<bool>((full_mask_ >> t) & 1)};
        tube_type_[n_tubes_++] = t;
    }
}

uint32_t Changer::payable_total_cents() const {
    uint32_t total = 0;
    for (uint8_t i = 0; i < n_tubes_; ++i) total += tubes_[i].cents * tubes_[i].count;
    return total;
}

void Changer::set_enabled(const uint32_t* cents, uint8_t n, bool accept) {
    accept_ = accept;
    state_.n_enabled = 0;
    if (accept) {
        for (uint8_t i = 0; i < n && i < 16; ++i) state_.enabled[state_.n_enabled++] = cents[i];
    }
    mask_dirty_ = true;
}

void Changer::reset() {
    if (payout_.active) interrupt_payout("reset");
    phase_ = Phase::Reset;
    mask_dirty_ = true;
    swallow_reset_ = false;
}

void Changer::note_miss() {
    if (offline_reported_ || link_.misses(kAddr) < kMissThreshold) return;
    offline_reported_ = true;
    state_.present = false;
    if (payout_.active) interrupt_payout("offline");
    update_status(Status::Offline, -1, "no response");
    phase_ = Phase::Reset;
    mask_dirty_ = true;
    last_attempt_ms_ = clock_.now_ms();
}

void Changer::enter_polling() {
    phase_ = Phase::Polling;
    last_tube_ms_ = clock_.now_ms();
    update_status(base_status(), -1, "");
}

void Changer::step() {
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
        case Phase::Features:
            do_features();
            break;
        case Phase::TubeStatus:
            do_tube_status();
            break;
        case Phase::CoinType:
            do_coin_type();
            break;
        case Phase::Polling:
            if (mask_dirty_) {
                do_coin_type();
            } else if (payout_.active) {
                do_payout();
            } else if (tubes_due_ || clock_.now_ms() - last_tube_ms_ >= kTubeRefreshMs) {
                do_tube_status();
            } else {
                do_poll();
            }
            break;
    }
    flush();
}

void Changer::do_reset() {
    Response r = link_.transact(kAddr, kCmdReset, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    offline_reported_ = false;
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Nak) return;
    swallow_reset_ = true;  // the changer announces its own reset (0x0B) on the first POLL
    phase_ = Phase::Setup;
}

void Changer::do_setup() {
    Response r = link_.transact(kAddr, kCmdSetup, nullptr, 0);
    if (r.kind != Reply::Data) {
        if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) note_miss();
        return;
    }
    link_.acknowledge();
    offline_reported_ = false;
    if (r.len < kSetupLen) return;
    const uint8_t level = r.data[0];
    scaling_ = r.data[3];
    routing_ = static_cast<uint16_t>((r.data[5] << 8) | r.data[6]);
    state_.n_denominations = 0;
    for (uint8_t t = 0; t < 16; ++t) {
        credit_[t] = r.data[7 + t];
        if (type_valid(t)) state_.denominations[state_.n_denominations++] = type_cents(t);
    }
    state_.level = static_cast<int8_t>(level >= 3 ? 3 : level);
    state_.present = true;
    alt_payout_ = false;
    rebuild_tubes();
    phase_ = level >= 3 ? Phase::Identify : Phase::TubeStatus;
}

void Changer::do_identify() {
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
        const bool alt = r.len >= 33 && (r.data[32] & 0x01);
        phase_ = alt ? Phase::Features : Phase::TubeStatus;
        return;
    }
    phase_ = Phase::TubeStatus;  // NAK: carry on without the optional features
}

void Changer::do_features() {
    const uint8_t d[5] = {kSubFeature, 0, 0, 0, 0x01};
    Response r = link_.transact(kAddr, kCmdExpansion, d, 5);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    alt_payout_ = (r.kind == Reply::Ack);
    phase_ = Phase::TubeStatus;
}

bool Changer::parse_tube_response(const Response& r) {
    if (r.kind != Reply::Data || r.len < kTubeLen) return false;
    full_mask_ = static_cast<uint16_t>((r.data[0] << 8) | r.data[1]);
    for (uint8_t t = 0; t < 16; ++t) counts_[t] = r.data[2 + t];
    rebuild_tubes();
    return true;
}

void Changer::do_tube_status() {
    Response r = link_.transact(kAddr, kCmdTube, nullptr, 0);
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (!parse_tube_response(r)) return;
    last_tube_ms_ = clock_.now_ms();
    tubes_due_ = false;
    if (phase_ == Phase::TubeStatus) {
        phase_ = Phase::CoinType;
    } else if (payout_.active && payout_.step == PayStep::L2Refresh) {
        uint32_t paid = 0;
        for (uint8_t t = 0; t < 16; ++t)
            if (type_tubed(t) && payout_.before[t] > counts_[t])
                paid += static_cast<uint32_t>(payout_.before[t] - counts_[t]) * type_cents(t);
        finish_payout(paid);
    }
}

void Changer::do_coin_type() {
    const uint16_t m = accept_ ? enable_mask() : 0;
    const uint8_t d[4] = {static_cast<uint8_t>(m >> 8), static_cast<uint8_t>(m & 0xFF), 0, 0};
    Response r = link_.transact(kAddr, kCmdCoinType, d, 4);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind != Reply::Ack) return;  // keep dirty, retry
    mask_dirty_ = false;
    if (phase_ == Phase::CoinType) enter_polling();
}

void Changer::do_poll() {
    Response r = link_.transact(kAddr, kCmdPoll, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    offline_reported_ = false;
    if (r.kind == Reply::Nak) return;
    const bool swallow = swallow_reset_;
    swallow_reset_ = false;
    if (r.kind == Reply::Ack) {
        update_status(base_status(), -1, "");
        return;
    }
    // Data: parse the whole block, then acknowledge it.
    bool have = false, was_reset = false;
    Status worst = Status::Ready;
    int16_t worst_byte = -1;
    for (uint8_t i = 0; i < r.len; ++i) {
        const uint8_t b = r.data[i];
        if (b & 0x80) {
            ++i;  // manual dispense report: two bytes, nothing to credit
        } else if ((b & 0xC0) == 0x40) {
            if (i + 1 >= r.len) break;
            const uint8_t type = b & 0x0F, rr = (b >> 4) & 0x03, count = r.data[++i];
            if (!type_valid(type) || rr > 1) continue;  // token, unknown type, or rejected coin
            PaymentEvent e = make_event(EventKind::CoinAccepted, Device::Coin);
            e.cents = type_cents(type);
            e.route = rr == 1 ? CoinRoute::Tubes : CoinRoute::Cashbox;
            push(e);
            if (rr == 1) {
                counts_[type] = count;
                rebuild_tubes();
            }
        } else if ((b & 0xF0) == 0x20) {
            // slug: not credited
        } else if (b >= 0x01 && b <= 0x0D) {
            Status s = Status::Error;
            switch (b) {
                case 0x01:
                    lever_ = true;
                    continue;
                case 0x02:
                case 0x0A:
                    s = Status::Busy;
                    break;
                case 0x06:
                    s = Status::Offline;
                    break;
                case 0x07:
                case 0x0C:
                    s = Status::Jammed;
                    break;
                case 0x0B:
                    if (swallow) continue;
                    was_reset = true;
                    s = Status::Busy;
                    break;
                default:
                    s = Status::Error;  // 03 04 05 08 09 0D
                    break;
            }
            if (!have || status_rank(s) > status_rank(worst)) {
                worst = s;
                worst_byte = b;
            }
            have = true;
        }
    }
    link_.acknowledge();
    if (have) {
        update_status(worst, worst_byte, was_reset ? "changer was reset" : "");
    } else {
        update_status(base_status(), -1, "");
    }
    if (was_reset) {
        if (payout_.active) interrupt_payout("changer_reset");
        mask_dirty_ = true;
        phase_ = Phase::Setup;
    }
}

bool Changer::request_payout(uint32_t cents, const char* request_id) {
    if (!state_.present || offline_reported_ || phase_ != Phase::Polling || payout_.active || cents == 0)
        return false;
    const bool l3 = state_.level >= 3 && alt_payout_;
    if (l3 && (scaling_ == 0 || cents % scaling_ != 0 || cents / scaling_ > 255)) return false;
    memset(&payout_, 0, sizeof(payout_));
    payout_.active = true;
    payout_.level3 = l3;
    payout_.step = l3 ? PayStep::L3Send : PayStep::L2Dispense;
    payout_.requested = cents;
    payout_.remaining = cents;
    payout_.start_ms = clock_.now_ms();
    copy_str(payout_.request_id, sizeof(payout_.request_id), request_id);
    for (uint8_t t = 0; t < 16; ++t) payout_.before[t] = payout_.working[t] = counts_[t];
    return true;
}

void Changer::finish_payout(uint32_t paid, const char* detail) {
    PaymentEvent e = make_event(EventKind::PayoutResult, Device::Coin);
    e.requested_cents = payout_.requested;
    copy_str(e.detail, sizeof(e.detail), detail);
    if (paid > payout_.requested) {
        paid = payout_.requested;
        copy_str(e.detail, sizeof(e.detail), "changer reported more than requested");
    }
    e.paid_cents = paid;
    e.payout_status = paid == payout_.requested ? PayoutStatus::Ok
                      : paid == 0               ? PayoutStatus::Failed
                                                : PayoutStatus::Partial;
    if (e.payout_status == PayoutStatus::Failed && e.detail[0] == '\0')
        copy_str(e.detail, sizeof(e.detail), "no coins paid");
    copy_str(e.request_id, sizeof(e.request_id), payout_.request_id);
    push(e);
    payout_.active = false;
    tubes_due_ = true;
}

// Ends an unfinished payout with the amount the driver can vouch for: level 3 the last PAYOUT VALUE
// POLL figure, level 2 the value of every DISPENSE the changer ACKed. Coins may already be in the
// customer's hand, so reporting 0 here would invite a double payment.
void Changer::interrupt_payout(const char* cause) {
    const uint32_t vouched = payout_.level3 ? payout_.progress_cents : payout_.requested - payout_.remaining;
    char detail[sizeof(PaymentEvent::detail)];
    copy_str(detail, sizeof(detail), "interrupted: ");
    size_t n = strlen(detail);
    copy_str(detail + n, sizeof(detail) - n, cause);
    finish_payout(vouched, detail);
}

void Changer::do_payout() {
    if (clock_.now_ms() - payout_.start_ms >= kPayoutTimeoutMs) {
        interrupt_payout("timeout");
        return;
    }
    Response r{};
    switch (payout_.step) {
        case PayStep::L3Send: {
            const uint8_t d[2] = {kSubPayout, static_cast<uint8_t>(payout_.requested / scaling_)};
            r = link_.transact(kAddr, kCmdExpansion, d, 2);
            if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) return note_miss();
            if (r.kind == Reply::Ack)
                payout_.step = PayStep::L3Value;
            else
                finish_payout(0);
            return;
        }
        case PayStep::L3Value: {
            const uint8_t d = kSubPayoutValue;
            r = link_.transact(kAddr, kCmdExpansion, &d, 1);
            if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) return note_miss();
            if (r.kind == Reply::Data) {
                link_.acknowledge();
                if (r.len >= 1) payout_.progress_cents = static_cast<uint32_t>(r.data[0]) * scaling_;
            }
            payout_.step = PayStep::L3Status;
            return;
        }
        case PayStep::L3Status: {
            const uint8_t d = kSubPayoutStatus;
            r = link_.transact(kAddr, kCmdExpansion, &d, 1);
            if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) return note_miss();
            if (r.kind == Reply::Data && r.len >= 16) {
                link_.acknowledge();
                uint32_t paid = 0;
                for (uint8_t t = 0; t < 16; ++t) paid += static_cast<uint32_t>(r.data[t]) * type_cents(t);
                finish_payout(paid);
                return;
            }
            if (r.kind == Reply::Data) link_.acknowledge();
            payout_.step = PayStep::L3Value;  // ACK = still busy
            return;
        }
        case PayStep::L2Dispense: {
            int best = -1;
            for (uint8_t t = 0; t < 16; ++t) {
                if (!type_tubed(t) || payout_.working[t] == 0 || type_cents(t) > payout_.remaining) continue;
                if (best < 0 || type_cents(t) > type_cents(static_cast<uint8_t>(best))) best = t;
            }
            if (best < 0) {
                payout_.step = PayStep::L2Refresh;
                return do_tube_status();
            }
            const uint8_t t = static_cast<uint8_t>(best);
            uint32_t n = payout_.remaining / type_cents(t);
            if (n > payout_.working[t]) n = payout_.working[t];
            if (n > 15) n = 15;
            const uint8_t d = static_cast<uint8_t>((n << 4) | t);
            r = link_.transact(kAddr, kCmdDispense, &d, 1);
            if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) return note_miss();
            if (r.kind == Reply::Ack) {
                payout_.working[t] = static_cast<uint16_t>(payout_.working[t] - n);
                payout_.remaining -= n * type_cents(t);
            } else {
                payout_.step = PayStep::L2Refresh;
            }
            return;
        }
        case PayStep::L2Refresh:
            return do_tube_status();
    }
}

}  // namespace mdbcore
