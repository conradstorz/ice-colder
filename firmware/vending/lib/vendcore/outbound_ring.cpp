#include "outbound_ring.h"

#include <string.h>

namespace vendcore {

void OutboundRing::erase_at(int pos) {
    for (int i = pos; i + 1 < count_; ++i) {
        e_[(head_ + i) % kCapacity] = e_[(head_ + i + 1) % kCapacity];
    }
    --count_;
}

void OutboundRing::pop() {
    if (count_ == 0) return;
    head_ = (head_ + 1) % kCapacity;
    --count_;
}

bool OutboundRing::push(const char* topic, const char* payload, size_t len, bool retain, Prio prio) {
    if (strlen(topic) >= sizeof(e_[0].topic) || len >= sizeof(e_[0].payload)) {
        ++dropped_;
        return false;
    }
    if (count_ == kCapacity) {
        // Evict the oldest entry of the lowest class strictly below the newcomer's.
        int victim = -1;
        for (int cls = 0; cls < static_cast<int>(prio) && victim < 0; ++cls) {
            for (int i = 0; i < count_; ++i) {
                if (static_cast<int>(e_[(head_ + i) % kCapacity].prio) == cls) {
                    victim = i;
                    break;
                }
            }
        }
        if (victim < 0 && prio == Prio::Outcome) victim = 0;  // all Outcomes: unavoidable, oldest goes
        if (victim < 0) {
            ++dropped_;
            return false;
        }
        erase_at(victim);
        ++dropped_;
    }
    RingEntry& d = e_[(head_ + count_) % kCapacity];
    memcpy(d.topic, topic, strlen(topic) + 1);
    memcpy(d.payload, payload, len);
    d.payload[len] = '\0';
    d.len = static_cast<uint16_t>(len);
    d.retain = retain;
    d.prio = prio;
    ++count_;
    return true;
}

}  // namespace vendcore
