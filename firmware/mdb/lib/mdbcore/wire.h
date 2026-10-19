// Contract JSON serializers of the MDB board. Pure, no heap; each returns bytes written
// (excluding NUL), or 0 when the output did not fit.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "boardcore/board_wire.h"
#include "events.h"

namespace mdbcore {

using boardcore::WireCtx;
using boardcore::topic_join;

constexpr const char* kSubsystem = "mdb";
extern const char* const kCommands[11];

// Enum strings exactly as the payment contract's schema enums.
const char* device_str(Device d);
const char* status_str(Status s);
const char* coin_route_str(CoinRoute r);
const char* bill_reason_str(BillReturnReason r);
const char* payout_status_str(PayoutStatus s);
const char* end_reason_str(SessionEndReason r);
const char* event_kind_str(EventKind k);

// payment/event envelope: {"event_id","boot_id","seq","ts","event":{"kind":...}}.
size_t event_json(const PaymentEvent& e, const char* event_id, const char* boot_id, uint32_t seq,
                  const char* iso_ts, char* out, size_t cap);

// Reads boot_id (copied NUL-terminated, at most 32 chars: boot_id_out must hold 33) and seq
// back out of an envelope. False when either is missing or malformed.
bool peek_boot_seq(const char* json, char* boot_id_out, uint32_t& seq_out);

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap);
size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap);
size_t lwt_json(char* out, size_t cap);

}  // namespace mdbcore
