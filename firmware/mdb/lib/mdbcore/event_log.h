// Write-ahead log of payment events: an envelope is appended to the store before it is
// published, and erased only when the edge acks it. Capacity 256 entries.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "events.h"
#include "ports.h"

namespace mdbcore {

class EventLog {
public:
    static constexpr int kCapacity = 256;

    EventLog(LogStore& store, IdSource& ids) : store_(store), ids_(ids) {}

    void begin_boot();  // mints boot_id, seq restarts at 1
    const char* boot_id() const { return boot_id_; }
    uint32_t seq_last() const { return seq_; }

    // Writes the envelope JSON to json_out and the store, then advances seq. False (seq
    // unchanged) when the log is full, the envelope does not fit, or the store refused.
    bool append(const PaymentEvent& e, const char* iso_ts, char* json_out, size_t cap);

    int pending() const { return store_.count(); }
    bool pending_at(int index, char* out, size_t cap) const { return store_.get(index, out, cap); }

    // Erases the maximal oldest prefix of entries with this boot_id and seq <= acked seq.
    // The store can only erase from the front, so an ack for the current boot cannot erase
    // anything while an older boot's unacked entries sit at the front; the edge acks those
    // first because they replay first (oldest boot first).
    void acked(const char* boot_id, uint32_t seq);

    bool full() const { return store_.count() >= kCapacity; }

private:
    LogStore& store_;
    IdSource& ids_;
    char boot_id_[17] = {};
    uint32_t seq_ = 0;
};

}  // namespace mdbcore
