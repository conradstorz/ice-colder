// Decodes one inbound command message (contract Command model:
// {"command", "request_id", "params"}). No heap use after static setup.
// decode_command uses one static arena and must be called from a single task
// (it is not reentrant).
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "profile.h"

namespace vendcore {

enum class CmdKind : uint8_t { Ping, SelfTest, ForceReport, Dispense, WaterValve, Unknown };

struct Command {
    CmdKind kind;
    char request_id[65];
    char name[33];
    uint8_t slot;
    Mechanism mechanism;
    SlotProfile profile;
    uint8_t valve_seconds;
};

enum class Decode : uint8_t { Ok, NoRequestId, Rejected, Unsupported };

struct Decoded {
    Decode result;
    Command cmd;
    char detail[96];
};

Decoded decode_command(const char* json, size_t len);

}  // namespace vendcore
