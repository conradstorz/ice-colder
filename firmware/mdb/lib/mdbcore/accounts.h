// Cash accounting held on the board: credit accepted and not yet sold, the one bill in
// escrow, and the last 32 payout outcomes (so a repeated request_id replays its result).
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "events.h"

namespace mdbcore {

class Accounts {
public:
    static constexpr int kPayouts = 32;

    void coin(uint32_t cents) { escrow_ += cents; }
    void bill_stacked(uint32_t cents) {
        escrow_ += cents;
        bill_pending_ = false;
        bill_cents_ = 0;
    }
    void bill_escrowed(uint32_t cents) {
        bill_pending_ = true;
        bill_cents_ = cents;
    }
    void bill_escrow_cleared() {
        bill_pending_ = false;
        bill_cents_ = 0;
    }
    uint32_t escrow_cents() const { return escrow_; }
    bool bill_in_escrow(uint32_t& cents) const {
        cents = bill_cents_;
        return bill_pending_;
    }
    // Deducts a sale; false (and credit zeroed) when it asked for more than was held.
    bool cash_sale(uint32_t cents);
    // Would stacking this bill take stacked credit over the cap? (escrowed bill not counted.)
    bool would_exceed(uint32_t bill_cents, uint32_t max_escrow) const {
        return static_cast<uint64_t>(escrow_) + bill_cents > max_escrow;
    }

    // false when request_id is already remembered (the first outcome is kept).
    bool remember_payout(const char* request_id, PayoutStatus status, uint32_t requested, uint32_t paid);
    bool find_payout(const char* request_id, PaymentEvent& replay) const;
    int payout_count() const { return count_; }
    // JSON array [{"request_id":..,"status":..,"requested":N,"paid":N},...], oldest first.
    void restore_payouts(const char* json, size_t len);
    size_t payouts_json(char* out, size_t cap) const;  // 0 when it did not fit

private:
    struct Payout {
        char id[65];
        PayoutStatus status;
        uint32_t requested, paid;
    };
    const Payout& at(int i) const { return ring_[(head_ + i) % kPayouts]; }

    uint32_t escrow_ = 0;
    bool bill_pending_ = false;
    uint32_t bill_cents_ = 0;
    Payout ring_[kPayouts] = {};
    int head_ = 0;  // index of the oldest entry
    int count_ = 0;
};

}  // namespace mdbcore
