#include "cashless.h"

#include <string.h>

namespace mdbcore {

namespace {

constexpr uint8_t kCmdReset = 0, kCmdSetup = 1, kCmdPoll = 2, kCmdVend = 3, kCmdReader = 4,
                  kCmdExpansion = 7;
constexpr uint8_t kSetupConfig = 0x00, kSetupPrices = 0x01;
constexpr uint8_t kVendRequest = 0x00, kVendCancel = 0x01, kVendSuccess = 0x02, kVendFailure = 0x03,
                  kVendComplete = 0x04, kVendCash = 0x05;
constexpr uint8_t kSubRequestId = 0x00, kSubFeatureEnable = 0x04;
constexpr uint8_t kConfigReplyLen = 8, kIdReplyLen = 30, kIdFeatureLen = 34;
constexpr uint8_t kFeatureAlwaysIdle = 0x02;
constexpr uint16_t kFundsUndisclosed = 0xFFFF;

void copy_str(char* dst, size_t cap, const char* src) {
    size_t i = 0;
    for (; src && src[i] && i + 1 < cap; ++i) dst[i] = src[i];
    dst[i] = '\0';
}

void copy_field(char* dst, size_t cap, const uint8_t* src, size_t n) {
    size_t len = n < cap - 1 ? n : cap - 1;
    while (len > 0 && (src[len - 1] == ' ' || src[len - 1] == 0)) --len;
    memcpy(dst, src, len);
    dst[len] = '\0';
}

}  // namespace

Cashless::Cashless(Link& link, Clock& clock, EventSink& sink, IdSource& ids)
    : link_(link), clock_(clock), sink_(sink), ids_(ids) {
    state_.present = false;
    state_.status = Status::Offline;
}

void Cashless::push(const PaymentEvent& e) {
    // A POLL block is at most 36 bytes and step() never parses a new block while pending_ is
    // non-empty, so 16 suffices. Overflow is counted, never silent.
    if (n_pending_ < kPending)
        pending_[n_pending_++] = e;
    else
        ++dropped_events_;
}

bool Cashless::flush() {
    uint8_t sent = 0;
    while (sent < n_pending_ && sink_.emit(pending_[sent])) ++sent;
    if (sent > 0) {
        for (uint8_t i = sent; i < n_pending_; ++i) pending_[i - sent] = pending_[i];
        n_pending_ = static_cast<uint8_t>(n_pending_ - sent);
    }
    return n_pending_ == 0;
}

void Cashless::update_status(Status s, int16_t mdb, const char* detail, bool force) {
    if (s == state_.status && !force) return;
    state_.status = s;
    PaymentEvent e = make_event(EventKind::DeviceStatus, Device::Cashless);
    e.status = s;
    e.mdb_status = mdb;
    copy_str(e.detail, sizeof(e.detail), detail);
    push(e);
}

PaymentEvent Cashless::session_event(EventKind kind) const {
    PaymentEvent e = make_event(kind, Device::Cashless);
    copy_str(e.session_id, sizeof(e.session_id), session_id_);
    return e;
}

void Cashless::emit_denied(const char* reason) {
    PaymentEvent e = session_event(EventKind::VendDenied);
    copy_str(e.request_id, sizeof(e.request_id), request_id_);
    copy_str(e.detail, sizeof(e.detail), reason);
    push(e);
}

void Cashless::deny_vend(const char* reason) {
    emit_denied(reason);
    vend_ = Vend::None;
    vend_sent_ = false;
    if (idle_vend_) clear_session();  // an always-idle vend has no reader session to fall back to
}

void Cashless::clear_session() {
    session_open_ = false;
    idle_vend_ = false;
    closing_ = false;
    funds_known_ = false;
    funds_cents_ = 0;
    vend_ = Vend::None;
    vend_sent_ = false;
    action_ = Action::None;
    follow_ = Action::None;
}

// The session is over: tell the Master. A vend request still awaiting its verdict is denied first.
void Cashless::lose_session(SessionEndReason why) {
    if (vend_ == Vend::Requested) emit_denied("session_ended");
    if (session_open_) {
        PaymentEvent e = session_event(EventKind::SessionEnded);
        e.end_reason = why;
        push(e);
    }
    clear_session();
}

void Cashless::queue(Action first, Action then) {
    action_ = first;
    follow_ = then;
}

void Cashless::begin_session(uint16_t funds_units) {
    if (session_open_) lose_session(SessionEndReason::Reader);
    ids_.fill_hex(session_id_, 16);
    session_id_[16] = '\0';
    session_open_ = true;
    closing_ = false;
    idle_vend_ = false;
    vend_ = Vend::None;
    funds_known_ = funds_units != kFundsUndisclosed;
    funds_cents_ = funds_known_ ? static_cast<uint32_t>(funds_units) * scale_ : 0;
    PaymentEvent e = session_event(EventKind::SessionBegun);
    e.funds_known = funds_known_;
    e.funds_cents = funds_cents_;
    push(e);
}

void Cashless::set_enabled(bool on) {
    enabled_ = on;
    enable_dirty_ = true;
}

void Cashless::reset() {
    lose_session(SessionEndReason::Reader);
    flush();
    cash_pending_ = false;
    phase_ = Phase::Reset;
    enable_dirty_ = true;
    swallow_reset_ = false;
    always_idle_active_ = false;
}

bool Cashless::session_open(char* session_id_out, bool& funds_known, uint32_t& funds_cents) const {
    funds_known = session_open_ && funds_known_;
    funds_cents = funds_cents_;
    if (!session_open_ || idle_vend_) return false;
    if (session_id_out) memcpy(session_id_out, session_id_, sizeof(session_id_));
    return true;
}

bool Cashless::vend_request(uint32_t cents, uint16_t item, const char* request_id) {
    if (phase_ != Phase::Polling || !enabled_ || closing_ || action_ != Action::None || vend_ != Vend::None)
        return false;
    if (!session_open_ && !always_idle_active_) return false;
    copy_str(request_id_, sizeof(request_id_), request_id);
    if (cents % scale_ != 0 || cents / scale_ > 0xFFFF) {
        emit_denied("price_scale");
        flush();
        return true;
    }
    if (!session_open_) {
        ids_.fill_hex(session_id_, 16);
        session_id_[16] = '\0';
        session_open_ = true;
        idle_vend_ = true;
        funds_known_ = false;
        funds_cents_ = 0;
    }
    vend_ = Vend::Requested;
    vend_sent_ = false;
    vend_cents_ = cents;
    vend_item_ = item;
    queue(Action::VendRequest, Action::None);
    return true;
}

bool Cashless::vend_result(bool success, uint16_t item) {
    if (vend_ != Vend::Approved || closing_ || action_ != Action::None) return false;
    result_item_ = item;
    vend_ = Vend::None;
    closing_ = true;
    end_reason_ = SessionEndReason::Complete;
    action_nak_ = false;
    queue(success ? Action::VendSuccess : Action::VendFailure, Action::SessionComplete);
    return true;
}

bool Cashless::end_session() {
    if (phase_ != Phase::Polling || !session_open_ || closing_ || vend_ != Vend::None ||
        action_ != Action::None)
        return false;
    closing_ = true;
    end_reason_ = SessionEndReason::Edge;
    action_nak_ = false;
    queue(Action::SessionComplete, Action::None);
    return true;
}

bool Cashless::cash_sale(uint32_t cents, uint16_t item) {
    if (phase_ != Phase::Polling || session_open_ || cash_pending_) return false;
    if (cents % scale_ != 0 || cents / scale_ > 0xFFFF) return false;
    cash_pending_ = true;
    cash_cents_ = cents;
    cash_item_ = item;
    return true;
}

void Cashless::note_miss() {
    if (offline_reported_ || link_.misses(kAddr) < kMissThreshold) return;
    offline_reported_ = true;
    state_.present = false;
    cash_pending_ = false;
    lose_session(SessionEndReason::Reader);
    update_status(Status::Offline, -1, "no response");
    phase_ = Phase::Reset;
    enable_dirty_ = true;
    last_attempt_ms_ = clock_.now_ms();
}

void Cashless::enter_polling() {
    phase_ = Phase::Polling;
    update_status(base_status(), -1, "");
}

void Cashless::step() {
    if (!flush()) return;  // never start a new transaction past an undelivered event
    switch (phase_) {
        case Phase::Inactive:
            return;
        case Phase::Reset:
            if (offline_reported_ && clock_.now_ms() - last_attempt_ms_ < kOfflineRetryMs) return;
            last_attempt_ms_ = clock_.now_ms();
            do_reset();
            break;
        case Phase::SetupConfig:
            do_setup_config();
            break;
        case Phase::SetupPrices:
            do_setup_prices();
            break;
        case Phase::RequestId:
            do_request_id();
            break;
        case Phase::FeatureEnable:
            do_feature_enable();
            break;
        case Phase::ReaderEnable:
            do_reader_enable();
            break;
        case Phase::Polling:
            if (vend_ == Vend::Approved && !closing_ && clock_.now_ms() - approved_ms_ >= edge_timeout_ms_) {
                // The Master never reported the vend: give the customer's funds back.
                vend_ = Vend::None;
                closing_ = true;
                end_reason_ = SessionEndReason::Timeout;
                queue(Action::VendFailure, Action::SessionComplete);
            }
            if (enable_dirty_) {
                do_reader_enable();
            } else if (action_ != Action::None) {
                do_action();
            } else if (cash_pending_) {
                do_cash_sale();
            } else {
                do_poll();
            }
            break;
    }
    flush();
}

void Cashless::do_reset() {
    Response r = link_.transact(kAddr, kCmdReset, nullptr, 0);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    offline_reported_ = false;
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Nak) return;
    swallow_reset_ = true;  // the reader announces its own reset (0x00) on the first POLL
    vmc_level_ = 1;
    always_idle_offered_ = false;
    always_idle_active_ = false;
    phase_ = Phase::SetupConfig;
}

void Cashless::do_setup_config() {
    const uint8_t d[5] = {kSetupConfig, vmc_level_, 0, 0, 0};  // VMC level, no display
    Response r = link_.transact(kAddr, kCmdSetup, d, 5);
    if (r.kind != Reply::Data) {
        if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) note_miss();
        return;
    }
    link_.acknowledge();
    offline_reported_ = false;
    if (r.len < kConfigReplyLen || r.data[0] != 0x01) return;
    const uint8_t level = r.data[1];
    if (level >= 3 && always_idle_wanted_ && vmc_level_ < 3) {
        vmc_level_ = 3;  // announce level 3 so the reader offers its optional features
        return;          // SETUP may be repeated while the reader is still disabled
    }
    scale_ = r.data[4] == 0 ? 1 : r.data[4];
    max_response_ms_ = static_cast<uint32_t>(r.data[6]) * 1000;
    state_.level = static_cast<int8_t>(level > 3 ? 3 : level);
    state_.present = true;
    phase_ = Phase::SetupPrices;
}

void Cashless::do_setup_prices() {
    const uint8_t d[5] = {kSetupPrices, 0xFF, 0xFF, 0x00, 0x00};  // max/min price unknown
    Response r = link_.transact(kAddr, kCmdSetup, d, 5);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Nak) return;
    phase_ = Phase::RequestId;
}

void Cashless::do_request_id() {
    uint8_t d[30];
    memset(d, '0', sizeof(d));
    d[0] = kSubRequestId;
    memcpy(d + 1, "ICE", 3);         // VMC manufacturer
    memcpy(d + 16, "mdb-s3      ", 12);  // model; the serial at d + 4 stays machine-neutral zeros
    d[28] = 0x00;                    // software version 0001
    d[29] = 0x01;
    Response r = link_.transact(kAddr, kCmdExpansion, d, sizeof(d));
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) {
        link_.acknowledge();
        if (r.len >= kIdReplyLen && r.data[0] == 0x09) {
            copy_field(state_.serial, sizeof(state_.serial), r.data + 4, 12);
            copy_field(state_.model, sizeof(state_.model), r.data + 16, 12);
            always_idle_offered_ = r.len >= kIdFeatureLen && vmc_level_ >= 3 &&
                                   (r.data[33] & kFeatureAlwaysIdle) != 0;
        }
    }
    // NAK: carry on without the optional identification.
    phase_ = (always_idle_offered_ && always_idle_wanted_) ? Phase::FeatureEnable : Phase::ReaderEnable;
}

void Cashless::do_feature_enable() {
    const uint8_t d[5] = {kSubFeatureEnable, 0x00, 0x00, 0x00, kFeatureAlwaysIdle};
    Response r = link_.transact(kAddr, kCmdExpansion, d, 5);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    always_idle_active_ = r.kind != Reply::Nak;
    phase_ = Phase::ReaderEnable;
}

void Cashless::do_reader_enable() {
    const uint8_t sub = enabled_ ? 0x01 : 0x00;
    Response r = link_.transact(kAddr, kCmdReader, &sub, 1);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    if (r.kind == Reply::Nak) return;  // keep dirty, retry
    enable_dirty_ = false;
    if (phase_ == Phase::ReaderEnable)
        enter_polling();
    else
        update_status(base_status(), -1, "");
}

void Cashless::do_action() {
    uint8_t d[5];
    uint8_t n = 1;
    switch (action_) {
        case Action::VendRequest: {
            const uint16_t price = static_cast<uint16_t>(vend_cents_ / scale_);
            d[0] = kVendRequest;
            d[1] = static_cast<uint8_t>(price >> 8);
            d[2] = static_cast<uint8_t>(price & 0xFF);
            d[3] = static_cast<uint8_t>(vend_item_ >> 8);
            d[4] = static_cast<uint8_t>(vend_item_ & 0xFF);
            n = 5;
            break;
        }
        case Action::VendCancel:
            d[0] = kVendCancel;
            break;
        case Action::VendSuccess:
            d[0] = kVendSuccess;
            d[1] = static_cast<uint8_t>(result_item_ >> 8);
            d[2] = static_cast<uint8_t>(result_item_ & 0xFF);
            n = 3;
            break;
        case Action::VendFailure:
            d[0] = kVendFailure;
            break;
        default:
            d[0] = kVendComplete;
            break;
    }
    Response r = link_.transact(kAddr, kCmdVend, d, n);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();  // the action stays queued and is retried
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    const Action done = action_;
    action_ = follow_;
    follow_ = Action::None;
    if (r.kind == Reply::Nak && done != Action::VendRequest) action_nak_ = true;
    if (done == Action::VendRequest) {
        if (r.kind == Reply::Nak)
            deny_vend("nak");
        else
            vend_sent_ = true;
    }
}

void Cashless::do_cash_sale() {
    const uint16_t price = static_cast<uint16_t>(cash_cents_ / scale_);
    const uint8_t d[5] = {kVendCash, static_cast<uint8_t>(price >> 8), static_cast<uint8_t>(price & 0xFF),
                          static_cast<uint8_t>(cash_item_ >> 8), static_cast<uint8_t>(cash_item_ & 0xFF)};
    Response r = link_.transact(kAddr, kCmdVend, d, 5);
    if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
        note_miss();
        return;
    }
    if (r.kind == Reply::Data) link_.acknowledge();
    cash_pending_ = false;  // the sale is a notification; a NAK is not retried
}

void Cashless::do_poll() {
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
    if (r.kind == Reply::Data) link_.acknowledge();

    bool was_reset = false, out_of_sequence = false, malfunction = false;
    uint8_t mal_code = 0;
    for (uint8_t i = 0; i < len;) {
        const uint8_t b = r.data[i];
        switch (b) {
            case 0x00:  // just reset
                if (!swallow) was_reset = true;
                i += 1;
                break;
            case 0x01:  // reader config data (only expected as the SETUP reply)
                i = static_cast<uint8_t>(i + kConfigReplyLen);
                break;
            case 0x03: {  // begin session
                if (i + 3 > len) {
                    i = len;
                    break;
                }
                begin_session(static_cast<uint16_t>((r.data[i + 1] << 8) | r.data[i + 2]));
                i += 3;
                break;
            }
            case 0x04:  // session cancel request: we must close it ourselves
                i += 1;
                if (!session_open_ || closing_ || vend_ == Vend::Approved) break;
                {
                    const bool sent = vend_ == Vend::Requested && vend_sent_;
                    if (vend_ == Vend::Requested) {
                        emit_denied("session_ended");
                        vend_ = Vend::None;
                        vend_sent_ = false;
                    }
                    closing_ = true;
                    end_reason_ = SessionEndReason::Reader;
                    if (sent)
                        queue(Action::VendCancel, Action::SessionComplete);
                    else
                        queue(Action::SessionComplete, Action::None);
                }
                break;
            case 0x05: {  // vend approved
                if (i + 3 > len) {
                    i = len;
                    break;
                }
                if (vend_ == Vend::Requested) {
                    const uint16_t units = static_cast<uint16_t>((r.data[i + 1] << 8) | r.data[i + 2]);
                    vend_ = Vend::Approved;
                    approved_ms_ = clock_.now_ms();
                    PaymentEvent e = session_event(EventKind::VendApproved);
                    copy_str(e.request_id, sizeof(e.request_id), request_id_);
                    e.cents = static_cast<uint32_t>(units) * scale_;
                    push(e);
                }
                i += 3;
                break;
            }
            case 0x06:  // vend denied
                if (vend_ == Vend::Requested) deny_vend("denied");
                i += 1;
                break;
            case 0x07:  // end session
                i += 1;
                if (session_open_) lose_session(closing_ ? end_reason_ : SessionEndReason::Reader);
                break;
            case 0x08:  // cancelled (reply to READER CANCEL)
                i += 1;
                break;
            case 0x0A:  // malfunction + code
                malfunction = true;
                mal_code = i + 1 < len ? r.data[i + 1] : 0;
                i += 2;
                break;
            case 0x0B:
                out_of_sequence = true;
                i += 1;
                break;
            default:  // display request, peripheral id, revalue: variable length, not used
                i = len;
                break;
        }
    }
    if (was_reset) {
        update_status(Status::Busy, 0x00, "reader was reset");
        lose_session(SessionEndReason::Reader);
        cash_pending_ = false;
        enable_dirty_ = true;
        phase_ = Phase::SetupConfig;
    } else if (out_of_sequence) {
        update_status(Status::Error, 0x0B, "command out of sequence");
        lose_session(SessionEndReason::Reader);
        cash_pending_ = false;
        enable_dirty_ = true;
        phase_ = Phase::Reset;
    } else if (malfunction) {
        update_status(Status::Error, mal_code, "reader malfunction");
    } else {
        update_status(base_status(), -1, "");
    }
}

}  // namespace mdbcore
