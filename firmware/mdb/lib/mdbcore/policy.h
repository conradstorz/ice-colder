// Edge-supplied acceptance policy and the reasons the board may be inhibited.
#pragma once

#include <stdint.h>

namespace mdbcore {

struct Policy {
    char policy_id[65];
    bool accept;
    uint32_t max_escrow_cents;
    uint32_t coin_enabled[16];
    uint8_t n_coin;
    uint32_t bill_enabled[16];
    uint8_t n_bill;
    bool cashless_enabled;
    uint32_t change_threshold_cents;
    uint16_t edge_timeout_s;
    bool return_bill_on_lever;
};

enum class Inhibit : uint8_t { None, NoPolicy, EdgeSilent, PolicyOff, DeviceFault };

}  // namespace mdbcore
