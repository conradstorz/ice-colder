// Generic board serializers shared by every board (topics, heartbeat, LWT, ack).
// Pure, no heap; each returns bytes written (excluding NUL), or 0 on overflow.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "boardcore/ack_cache.h"

namespace boardcore {

struct WireCtx {
    const char* machine_id = "";
    const char* hardware_id = "";
    const char* ip = "";
    const char* firmware = "";
    const char* iso_time = nullptr;  // ISO-8601 UTC, or nullptr to omit "timestamp" (no NTP yet)
};

// "vmc/<machine_id>/<tail>".
size_t topic_join(const WireCtx& ctx, const char* tail, char* out, size_t cap);
size_t heartbeat_json(const char* subsystem, uint32_t uptime_s, char* out, size_t cap);
size_t lwt_json(const char* subsystem, char* out, size_t cap);
size_t ack_json(const Ack& a, char* out, size_t cap);

}  // namespace boardcore
