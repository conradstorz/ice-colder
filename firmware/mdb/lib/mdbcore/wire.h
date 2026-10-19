// Contract JSON serializers of the MDB board. Pure, no heap; each returns bytes written
// (excluding NUL), or 0 when the output did not fit.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "boardcore/board_wire.h"

namespace mdbcore {

using boardcore::WireCtx;
using boardcore::topic_join;

constexpr const char* kSubsystem = "mdb";
extern const char* const kCommands[11];

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap);
size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap);
size_t lwt_json(char* out, size_t cap);

}  // namespace mdbcore
