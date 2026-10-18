#include "boardcore/board_wire.h"

#include <stdio.h>

#include "boardcore/json_writer.h"

namespace boardcore {
namespace {

const char* ack_status(AckStatus s) {
    switch (s) {
        case AckStatus::Ok: return "ok";
        case AckStatus::Rejected: return "rejected";
        case AckStatus::Failed: return "failed";
        case AckStatus::Unsupported: return "unsupported";
    }
    return "failed";
}

}  // namespace

size_t topic_join(const WireCtx& ctx, const char* tail, char* out, size_t cap) {
    const int n = snprintf(out, cap, "vmc/%s/%s", ctx.machine_id, tail);
    return (n > 0 && static_cast<size_t>(n) < cap) ? static_cast<size_t>(n) : 0;
}

size_t heartbeat_json(const char* subsystem, uint32_t uptime_s, char* out, size_t cap) {
    JsonWriter w(out, cap);
    w.ch('{');
    w.field("subsystem", subsystem);
    w.ch(',');
    w.key("uptime_seconds");
    w.num(static_cast<long>(uptime_s));
    w.ch('}');
    return w.done();
}

size_t lwt_json(const char* subsystem, char* out, size_t cap) {
    JsonWriter w(out, cap);
    w.ch('{');
    w.field("subsystem", subsystem);
    w.ch(',');
    w.key("uptime_seconds");
    w.num(-1);
    w.ch('}');
    return w.done();
}

size_t ack_json(const Ack& a, char* out, size_t cap) {
    JsonWriter w(out, cap);
    w.ch('{');
    w.field("request_id", a.request_id);
    w.ch(',');
    w.field("command", a.command);
    w.ch(',');
    w.field("status", ack_status(a.status));
    w.ch(',');
    w.field("phase", a.phase == Phase::Accepted ? "accepted" : "completed");
    if (a.detail[0] != '\0') {
        w.ch(',');
        w.field("detail", a.detail);
    }
    if (a.result_json[0] != '\0') {
        w.ch(',');
        w.key("result");
        w.raw(a.result_json);
    }
    w.ch('}');
    return w.done();
}

}  // namespace boardcore
