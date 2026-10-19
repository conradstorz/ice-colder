// Decodes one inbound cmd/mdb message (SubsystemCommand: request_id, command, params,
// timestamp) into the Master's Command, validating every bound of the payment schemas.
// No heap after static setup. decode_command uses one static arena and must be called from a single
// task (it is not reentrant). decode_event_ack is a hand parser with no shared state: it is safe to
// call from the MQTT client task and never blocks.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "master.h"

namespace mdbcore {

enum class Decode : uint8_t { Ok, NoRequestId, Rejected, Unsupported };

struct Decoded {
    Decode result;
    Command cmd;
    char detail[96];
};

// sale_id (payout) and session_id (vend_request, vend_result, end_session) are both capped at 64
// characters (the schema maxLength); a longer one is Rejected, never truncated.
// NoRequestId: not a JSON object, or request_id missing / not 8-64 chars (nothing to ack to).
// Rejected: a bound violated or a float/string where an integer is required; detail names the
// field. Unsupported: unknown command. A missing timestamp leaves timestamp_unix at -1.
Decoded decode_command(const char* json, size_t len);

// payment/event/ack: {"boot_id" (8-32 chars), "seq" (integer >= 1)}. boot_id must hold 33.
bool decode_event_ack(const char* json, size_t len, char* boot_id, uint32_t& seq);

}  // namespace mdbcore
