// Replay cache of the last 32 acks, keyed by request_id (contract: a retried
// command gets the cached ack back instead of re-running the handler).
#pragma once

#include <stdint.h>

namespace vendcore {

enum class AckStatus : uint8_t { Ok, Rejected, Failed, Unsupported };
enum class Phase : uint8_t { Accepted, Completed };

struct Ack {
    char request_id[65];
    char command[33];
    AckStatus status;
    Phase phase;
    char detail[96];
    char result_json[512];
};

class AckCache {
public:
    static constexpr int kCapacity = 32;
    const Ack* find(const char* request_id) const;
    // Same request_id overwrites in place; otherwise evicts the oldest.
    void put(const Ack& a);

private:
    Ack entries_[kCapacity] = {};
    int count_ = 0;
    int next_ = 0;  // slot the next new entry takes (the oldest once full)
};

}  // namespace vendcore
