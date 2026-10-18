// Fixed 64-entry hold for publishes made while the broker is unreachable.
// When full it evicts the oldest entry of the lowest priority class below the
// newcomer's (Telemetry, then Normal); an Outcome is never evicted for a
// lower-priority newcomer, and a lower-priority newcomer is dropped instead.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace boardcore {

enum class Prio : uint8_t { Telemetry = 0, Normal = 1, Outcome = 2 };

struct RingEntry {
    char topic[96];
    char payload[256];
    uint16_t len;
    bool retain;
    Prio prio;
};

class OutboundRing {
public:
    static constexpr int kCapacity = 64;
    // False when the entry was too big for a slot or was itself dropped.
    bool push(const char* topic, const char* payload, size_t len, bool retain, Prio prio);
    const RingEntry* front() const { return count_ > 0 ? &e_[head_] : nullptr; }
    void pop();
    int size() const { return count_; }
    uint32_t dropped() const { return dropped_; }

private:
    RingEntry e_[kCapacity] = {};
    int head_ = 0;
    int count_ = 0;
    uint32_t dropped_ = 0;
    void erase_at(int pos);  // pos = offset from head
};

}  // namespace boardcore
