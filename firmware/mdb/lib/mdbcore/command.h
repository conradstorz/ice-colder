// Decodes one inbound cmd/mdb message (SubsystemCommand: request_id, command, params,
// timestamp) into the Master's Command, validating every bound of the payment schemas.
// No heap after static setup. decode_command and decode_event_ack share one static arena and
// must be called from a single task (they are not reentrant).
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

// NoRequestId: not a JSON object, or request_id missing / not 8-64 chars (nothing to ack to).
// Rejected: a bound violated or a float/string where an integer is required; detail names the
// field. Unsupported: unknown command. A missing timestamp leaves timestamp_unix at -1.
Decoded decode_command(const char* json, size_t len);

// payment/event/ack: {"boot_id" (8-32 chars), "seq" (integer >= 1)}. boot_id must hold 33.
bool decode_event_ack(const char* json, size_t len, char* boot_id, uint32_t& seq);

}  // namespace mdbcore
