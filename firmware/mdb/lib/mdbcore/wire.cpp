#include "wire.h"

#include "boardcore/json_writer.h"
#include "version.h"

namespace mdbcore {

const char* const kCommands[11] = {"ping",          "self_test",    "force_report", "set_policy",
                                   "payout",        "vend_request", "vend_result",  "end_session",
                                   "return_escrow", "device_reset", "cash_sale"};

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap) {
    boardcore::JsonWriter w(out, cap);
    w.ch('{');
    w.field("subsystem", kSubsystem);
    w.ch(',');
    w.field("firmware", ctx.firmware);
    w.ch(',');
    w.field("contract_version", kContractVersion);
    w.ch(',');
    w.field("brand", "ice-colder");
    w.ch(',');
    w.field("model", "mdb-s3");
    w.ch(',');
    w.field("hardware_id", ctx.hardware_id);
    w.ch(',');
    w.field("ip", ctx.ip);
    w.ch(',');
    w.raw("\"channels\":[],");
    w.key("commands");
    w.ch('[');
    for (int i = 0; i < 11; ++i) {
        if (i > 0) w.ch(',');
        w.str(kCommands[i]);
    }
    w.ch(']');
    if (ctx.iso_time != nullptr) {
        w.ch(',');
        w.field("timestamp", ctx.iso_time);
    }
    w.ch('}');
    return w.done();
}

size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap) {
    return boardcore::heartbeat_json(kSubsystem, uptime_s, out, cap);
}

size_t lwt_json(char* out, size_t cap) { return boardcore::lwt_json(kSubsystem, out, cap); }

}  // namespace mdbcore
