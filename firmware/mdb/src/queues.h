// Queue item types shared by the bus, ledger and comms tasks (spec section 5.1).
//   cmd_q    CmdItem        comms  -> bus      (8)
//   evt_q    PaymentEvent   bus    -> ledger   (64)  a full queue defers polling, never drops
//   pub_q    PubItem        ledger -> comms    (32)
//   ack_q    boardcore::Ack bus    -> comms    (16)
//   ack_in_q EventAckItem   comms  -> ledger   (8)
// plus the 1-deep state mailbox (StateMsg, overwrite) from the bus task to the ledger.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "boardcore/ack_cache.h"
#include "boardcore/outbound_ring.h"
#include "command.h"
#include "events.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "state.h"

struct CmdItem {
    enum Kind { Decoded, EdgeSeen, ForceReport, WallTime } kind;
    mdbcore::Decoded decoded;
    int64_t wall_unix;  // WallTime
};

// payment/state is about 0.8 KB and grows with the tube list (1.6 KB worst case), so the payload
// is larger than the 512 B the plan named. 32 items is ~54 KB, allocated once at boot.
constexpr size_t kPubPayloadMax = 1664;
struct PubItem {
    char topic[96];
    char payload[kPubPayloadMax];
    uint32_t replay_epoch;  // Outcome items: ledger's replay epoch when queued; comms drops stale ones
    uint16_t len;
    bool retain;
    boardcore::Prio prio;
};

struct EventAckItem {
    char boot_id[33];
    uint32_t seq;
};

// Counters kept by the bus task instead of log lines (it must not log): the ledger reports them.
struct BusStats {
    uint32_t ack_dropped;           // ack lost to a full ack_q after one was already held
    uint32_t ack_held;              // ack held for retry because ack_q was full
    uint32_t payouts_unserialized;  // payout store did not fit its buffer
    uint32_t rx_mode_disagreements;
    uint32_t rx_ambiguous;
    uint32_t rx_orphans;
    uint32_t rx_lost;
    uint32_t corrupt_blocks;
    uint32_t flash_window_timeouts;  // ledger overran the flash quiet window
};

// A deep copy of the Master's PaymentState (whose pointers refer into the Master) plus the
// payout idempotency store, so the ledger task never touches the bus task's objects.
constexpr size_t kPayoutsMax = 5120;
struct StateMsg {
    mdbcore::PaymentState state;
    char policy_id[65];
    char session_ts[25];
    BusStats stats;
    size_t payouts_len;
    char payouts[kPayoutsMax];
};

struct Queues {
    QueueHandle_t cmd_q, evt_q, pub_q, ack_q, ack_in_q, state_q;
};
