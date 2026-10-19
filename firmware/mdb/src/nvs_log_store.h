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
// Only the ledger task calls append/get/erase_first/save_payouts; count() is safe from any task.
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
    int count() const override { return static_cast<int>(tail_.load() - head_.load()); }
    bool get(int index, char* out, size_t cap) const override;
    void erase_first(int n) override;

    // Payout idempotency store (Accounts::payouts_json). load: false when none stored.
    bool load_payouts(char* out, size_t cap, size_t& len) const;
    bool save_payouts(const char* json, size_t len);

private:
    static void key_for(uint32_t index, char* out);

    nvs_handle_t h_ = 0;
    bool ok_ = false;
    std::atomic<uint32_t> head_{0};
    std::atomic<uint32_t> tail_{0};
};
