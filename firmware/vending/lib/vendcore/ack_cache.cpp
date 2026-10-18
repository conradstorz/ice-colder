#include "ack_cache.h"

#include <string.h>

namespace vendcore {

const Ack* AckCache::find(const char* request_id) const {
    if (request_id == nullptr) return nullptr;
    for (int i = 0; i < count_; ++i) {
        if (strcmp(entries_[i].request_id, request_id) == 0) return &entries_[i];
    }
    return nullptr;
}

void AckCache::put(const Ack& a) {
    for (int i = 0; i < count_; ++i) {
        if (strcmp(entries_[i].request_id, a.request_id) == 0) {
            entries_[i] = a;
            return;
        }
    }
    entries_[next_] = a;
    next_ = (next_ + 1) % kCapacity;
    if (count_ < kCapacity) ++count_;
}

}  // namespace vendcore
