// The replay log's flash store: NVS partition "evlog", namespace "log".
//
// Scheme. Entry i is the blob under key "e<8 hex of i>". Two counters in the same namespace,
// "head" (index of the oldest entry) and "tail" (index the next append takes), only ever grow;
// count() = tail - head. append() writes the blob, then advances tail, so a power cut between the
// two leaves at most an unreferenced blob that the next append overwrites (a duplicate, never a
// hole). erase_first(n) advances head FIRST and then erases the blobs: a power cut in between
// leaves orphan blobs below head (swept at the next begin()), never a counted entry whose blob is
// gone, which would wedge EventLog::acked at the front forever. The idempotency store for payouts
// lives in the same namespace under "payouts".
//
// erase_first(n) is cheap: it only advances `head` (one flash write). The blobs below head are
// erased later, a few at a time, by sweep_step(), so one ack can never turn into a long run of
// flash writes inside a quiet window (see flash_window.h). begin() sweeps every stale blob left by
// a cut, by walking the whole namespace, not just a window below head.
//
// Only the ledger task calls append/get/erase_first/sweep_step/save_payouts; count() is safe from
// any task. Every call that writes flash must run inside a flash quiet window.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <atomic>

#include "nvs.h"
#include "ports.h"

class NvsLogStore : public mdbcore::LogStore {
public:
    // Opens (erasing a corrupt partition) and reads head/tail. False when flash is unusable;
    // append() then refuses.
    bool begin();

    bool append(const char* json, size_t len) override;
    // head is read before tail: both only grow and head <= tail always, so this order can never
    // observe head > tail (a torn read the other way round could go negative).
    int count() const override {
        const uint32_t h = head_.load();
        const uint32_t t = tail_.load();
        return t >= h ? static_cast<int>(t - h) : 0;
    }
    bool get(int index, char* out, size_t cap) const override;
    void erase_first(int n) override;

    // Erases up to max_keys stale blobs below head (see above). Returns how many it erased.
    int sweep_step(int max_keys);
    int sweep_pending() const { return static_cast<int>(head_.load() - swept_); }

    // Payout idempotency store (Accounts::payouts_json). load: false when none stored.
    bool load_payouts(char* out, size_t cap, size_t& len) const;
    bool save_payouts(const char* json, size_t len);

private:
    static void key_for(uint32_t index, char* out);

    nvs_handle_t h_ = 0;
    bool ok_ = false;
    std::atomic<uint32_t> head_{0};
    std::atomic<uint32_t> tail_{0};
    uint32_t swept_ = 0;  // blobs below this index are already erased; ledger task only
};
