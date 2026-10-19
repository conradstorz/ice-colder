// Snapshot of the board's payment state (the payment/state document, before serialization).
#pragma once

#include <stdint.h>

#include "device_state.h"
#include "events.h"
#include "policy.h"

namespace mdbcore {

struct PaymentState {
    const char* boot_id;   // not owned by the Master: the EventLog owner fills boot_id and seq_last
    uint32_t seq_last;
    const char* policy_id;  // nullptr = null; points into the Master, valid while it lives
    bool accepting;         // true iff inhibited == Inhibit::None
    Inhibit inhibited;
    uint32_t escrow_cents;
    bool bill_in_escrow;
    uint32_t bill_in_escrow_cents;
    DeviceState devices[3];  // indexed by Device: Coin, Bill, Cashless
    TubeLevel tubes[16];
    uint8_t n_tubes;
    bool exact_change;
    bool stacker_known;
    uint16_t stacker_count;
    bool stacker_full;
    bool session_open;
    char session_id[65];
    bool funds_known;
    uint32_t funds_cents;
    // ISO-8601 UTC of the session start (wall clock when set_wall_time was called, boot clock
    // otherwise); nullptr when no session is open. Points into the Master.
    const char* session_started_ts;
};

}  // namespace mdbcore
