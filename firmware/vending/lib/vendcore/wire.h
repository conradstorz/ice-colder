// Contract JSON serializers (topics and payloads of the vending board). Pure,
// no heap; every function returns bytes written (excluding NUL), or 0 when the
// output did not fit (or, for topic_for/payload_for, when the event has no topic).
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "ack_cache.h"
#include "ports.h"

namespace vendcore {

struct WireCtx {
    const char* machine_id = "";
    const char* hardware_id = "";
    const char* ip = "";
    const char* firmware = "";
    const char* iso_time = nullptr;  // ISO-8601 UTC, or nullptr to omit "timestamp" (no NTP yet)
};

// "vmc/<machine_id>/<tail>".
size_t topic_join(const WireCtx& ctx, const char* tail, char* out, size_t cap);

// Health events have no contract topic: returns 0.
size_t topic_for(const Event& e, const WireCtx& ctx, char* out, size_t cap);
size_t payload_for(const Event& e, const WireCtx& ctx, uint32_t uptime_s, char* out, size_t cap);

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap);
size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap);
size_t lwt_json(char* out, size_t cap);
size_t ack_json(const Ack& a, char* out, size_t cap);

int ha_discovery_count();
size_t ha_discovery(int i, const WireCtx& ctx, char* topic, size_t tcap, char* payload, size_t pcap);

}  // namespace vendcore
