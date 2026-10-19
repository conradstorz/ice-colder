// Contract JSON serializers of the MDB board. Pure, no heap; each returns bytes written
// (excluding NUL), or 0 when the output did not fit.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "boardcore/board_wire.h"
#include "events.h"
#include "policy.h"
#include "state.h"

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
// nullptr for Inhibit::None (serialized as JSON null).
const char* inhibit_str(Inhibit i);

// payment/event envelope: {"event_id","boot_id","seq","ts","event":{"kind":...}}.
size_t event_json(const PaymentEvent& e, const char* event_id, const char* boot_id, uint32_t seq,
                  const char* iso_ts, char* out, size_t cap);

// payment/state document (payment_state.schema.json). `accepting` is derived from
// `inhibited` (never from s.accepting) so the two cannot disagree; enabled denominations are
// clipped to the declared ones and tubes with cents == 0 are omitted. 0 when it did not fit.
size_t state_json(const PaymentState& s, const char* iso_ts, char* out, size_t cap);

// vmc/<machine_id>/payment/event, payment/state, payment/event/ack, cmd/mdb, cmd/mdb/ack.
size_t topic_event(const WireCtx& ctx, char* out, size_t cap);
size_t topic_state(const WireCtx& ctx, char* out, size_t cap);
size_t topic_event_ack(const WireCtx& ctx, char* out, size_t cap);
size_t topic_cmd(const WireCtx& ctx, char* out, size_t cap);
size_t topic_ack(const WireCtx& ctx, char* out, size_t cap);

// Reads boot_id (copied NUL-terminated, at most 32 chars: boot_id_out must hold 33) and seq
// back out of an envelope. False when either is missing or malformed.
bool peek_boot_seq(const char* json, char* boot_id_out, uint32_t& seq_out);

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap);
size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap);
size_t lwt_json(char* out, size_t cap);

}  // namespace mdbcore
