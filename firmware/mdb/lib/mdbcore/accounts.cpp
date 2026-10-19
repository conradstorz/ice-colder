#include "accounts.h"

#include <string.h>

#include "boardcore/json_writer.h"
#include "wire.h"

namespace mdbcore {

bool Accounts::cash_sale(uint32_t cents) {
    if (cents > escrow_) {
        escrow_ = 0;
        return false;
    }
    escrow_ -= cents;
    return true;
}

bool Accounts::remember_payout(const char* request_id, PayoutStatus status, uint32_t requested,
                               uint32_t paid) {
    if (request_id == nullptr || strlen(request_id) > 64) return false;
    for (int i = 0; i < count_; ++i) {
        if (strcmp(at(i).id, request_id) == 0) return false;
    }
    int slot;
    if (count_ < kPayouts) {
        slot = (head_ + count_) % kPayouts;
        ++count_;
    } else {
        slot = head_;  // evict the oldest
        head_ = (head_ + 1) % kPayouts;
    }
    Payout& p = ring_[slot];
    strcpy(p.id, request_id);
    p.status = status;
    p.requested = requested;
    p.paid = paid;
    return true;
}

bool Accounts::find_payout(const char* request_id, PaymentEvent& replay) const {
    if (request_id == nullptr) return false;
    for (int i = 0; i < count_; ++i) {
        const Payout& p = at(i);
        if (strcmp(p.id, request_id) != 0) continue;
        replay = make_event(EventKind::PayoutResult, Device::Coin);
        strcpy(replay.request_id, p.id);
        replay.payout_status = p.status;
        replay.requested_cents = p.requested;
        replay.paid_cents = p.paid;
        return true;
    }
    return false;
}

size_t Accounts::payouts_json(char* out, size_t cap) const {
    boardcore::JsonWriter w(out, cap);
    w.ch('[');
    for (int i = 0; i < count_; ++i) {
        const Payout& p = at(i);
        if (i > 0) w.ch(',');
        w.ch('{');
        w.field("request_id", p.id);
        w.ch(',');
        w.field("status", payout_status_str(p.status));
        w.raw(",\"requested\":");
        w.num(static_cast<long>(p.requested));
        w.raw(",\"paid\":");
        w.num(static_cast<long>(p.paid));
        w.ch('}');
    }
    w.ch(']');
    return w.done();
}

namespace {
// Finds `"key":` within [from, end); returns the pointer just past the colon.
const char* find_key(const char* from, const char* end, const char* key) {
    char pat[24];
    const size_t kl = strlen(key);
    if (kl + 4 > sizeof(pat)) return nullptr;
    pat[0] = '"';
    memcpy(pat + 1, key, kl);
    pat[kl + 1] = '"';
    pat[kl + 2] = ':';
    pat[kl + 3] = '\0';
    const size_t pl = kl + 3;
    for (const char* p = from; p + pl <= end; ++p) {
        if (memcmp(p, pat, pl) == 0) return p + pl;
    }
    return nullptr;
}

bool read_uint(const char* p, const char* end, uint32_t& out) {
    if (p >= end || *p < '0' || *p > '9') return false;
    uint64_t v = 0;
    while (p < end && *p >= '0' && *p <= '9') {
        v = v * 10 + static_cast<uint64_t>(*p - '0');
        if (v > 0xFFFFFFFFull) return false;
        ++p;
    }
    out = static_cast<uint32_t>(v);
    return true;
}
}  // namespace

void Accounts::restore_payouts(const char* json, size_t len) {
    head_ = 0;
    count_ = 0;
    if (json == nullptr) return;
    const char* end = json + len;
    const char* cur = json;
    while (true) {
        const char* id = find_key(cur, end, "request_id");
        if (id == nullptr || id >= end || *id != '"') return;
        ++id;
        const char* q = id;
        while (q < end && *q != '"') ++q;
        if (q >= end || static_cast<size_t>(q - id) > 64) return;
        char rid[65];
        memcpy(rid, id, static_cast<size_t>(q - id));
        rid[q - id] = '\0';
        // Bound this entry's other fields by its closing brace.
        const char* close = q;
        while (close < end && *close != '}') ++close;
        const char* st = find_key(q, close, "status");
        if (st == nullptr || st >= close || *st != '"') return;
        ++st;
        PayoutStatus status = PayoutStatus::Failed;
        bool known = false;
        for (uint8_t s = 0; s < 3; ++s) {
            const char* name = payout_status_str(static_cast<PayoutStatus>(s));
            const size_t nl = strlen(name);
            if (static_cast<size_t>(close - st) > nl && memcmp(st, name, nl) == 0 && st[nl] == '"') {
                status = static_cast<PayoutStatus>(s);
                known = true;
            }
        }
        uint32_t req = 0, paid = 0;
        const char* rq = find_key(q, close, "requested");
        const char* pd = find_key(q, close, "paid");
        if (known && rq != nullptr && pd != nullptr && read_uint(rq, close, req) &&
            read_uint(pd, close, paid)) {
            remember_payout(rid, status, req, paid);
        }
        cur = close < end ? close + 1 : end;
    }
}

}  // namespace mdbcore
