#include "wire.h"

#include "boardcore/json_writer.h"
#include "version.h"

namespace mdbcore {

const char* const kCommands[11] = {"ping",          "self_test",    "force_report", "set_policy",
                                   "payout",        "vend_request", "vend_result",  "end_session",
                                   "return_escrow", "device_reset", "cash_sale"};

const char* device_str(Device d) {
    static const char* const t[] = {"coin", "bill", "cashless"};
    return t[static_cast<uint8_t>(d)];
}
const char* status_str(Status s) {
    static const char* const t[] = {"ready", "disabled", "busy", "jammed", "full",
                                    "empty", "tilt",     "offline", "error"};
    return t[static_cast<uint8_t>(s)];
}
const char* coin_route_str(CoinRoute r) { return r == CoinRoute::Tubes ? "tubes" : "cashbox"; }
const char* bill_reason_str(BillReturnReason r) {
    static const char* const t[] = {"policy", "edge_silent", "customer", "edge", "timeout"};
    return t[static_cast<uint8_t>(r)];
}
const char* payout_status_str(PayoutStatus s) {
    static const char* const t[] = {"ok", "partial", "failed"};
    return t[static_cast<uint8_t>(s)];
}
const char* end_reason_str(SessionEndReason r) {
    static const char* const t[] = {"complete", "cancelled", "reader", "edge", "timeout"};
    return t[static_cast<uint8_t>(r)];
}
const char* event_kind_str(EventKind k) {
    static const char* const t[] = {
        "coin_accepted", "bill_escrowed", "bill_stacked", "bill_returned",
        "escrow_return_pressed", "payout_result", "session_begun", "vend_approved",
        "vend_denied", "session_ended", "device_status", "cash_collected"};
    return t[static_cast<uint8_t>(k)];
}

const char* inhibit_str(Inhibit i) {
    switch (i) {
        case Inhibit::None: return nullptr;
        case Inhibit::NoPolicy: return "no_policy";
        case Inhibit::EdgeSilent: return "edge_silent";
        case Inhibit::PolicyOff: return "policy";
        case Inhibit::DeviceFault: return "device_fault";
    }
    return "device_fault";
}

size_t event_json(const PaymentEvent& e, const char* event_id, const char* boot_id, uint32_t seq,
                  const char* iso_ts, char* out, size_t cap) {
    boardcore::JsonWriter w(out, cap);
    w.ch('{');
    w.field("event_id", event_id);
    w.ch(',');
    w.field("boot_id", boot_id);
    w.raw(",\"seq\":");
    w.num(static_cast<long>(seq));
    w.ch(',');
    w.field("ts", iso_ts);
    w.raw(",\"event\":{");
    w.field("kind", event_kind_str(e.kind));
    auto num = [&w](const char* k, uint32_t v) {
        w.ch(',');
        w.key(k);
        w.num(static_cast<long>(v));
    };
    auto text = [&w](const char* k, const char* v) {
        w.ch(',');
        w.field(k, v);
    };
    switch (e.kind) {
        case EventKind::CoinAccepted:
            num("cents", e.cents);
            text("routed", coin_route_str(e.route));
            break;
        case EventKind::BillEscrowed:
        case EventKind::BillStacked:
            num("cents", e.cents);
            break;
        case EventKind::BillReturned:
            num("cents", e.cents);
            text("reason", bill_reason_str(e.bill_reason));
            break;
        case EventKind::EscrowReturnPressed:
            break;
        case EventKind::PayoutResult:
            text("request_id", e.request_id);
            num("requested_cents", e.requested_cents);
            num("paid_cents", e.paid_cents);
            text("status", payout_status_str(e.payout_status));
            if (e.detail[0] != '\0') text("detail", e.detail);
            break;
        case EventKind::SessionBegun:
            text("session_id", e.session_id);
            if (e.funds_known) {
                num("funds_cents", e.funds_cents);
            } else {
                w.raw(",\"funds_cents\":null");
            }
            break;
        case EventKind::VendApproved:
            text("session_id", e.session_id);
            text("request_id", e.request_id);
            num("cents", e.cents);
            break;
        case EventKind::VendDenied:
            text("session_id", e.session_id);
            text("request_id", e.request_id);
            text("reason", e.detail);
            break;
        case EventKind::SessionEnded:
            text("session_id", e.session_id);
            text("reason", end_reason_str(e.end_reason));
            break;
        case EventKind::DeviceStatus:
            text("device", device_str(e.device));
            text("status", status_str(e.status));
            if (e.mdb_status >= 0) {
                num("mdb_status", static_cast<uint32_t>(e.mdb_status));
            } else {
                w.raw(",\"mdb_status\":null");
            }
            break;
        case EventKind::CashCollected:
            text("device", device_str(e.device));
            break;
    }
    w.raw("}}");
    return w.done();
}

namespace {

void write_cents_array(boardcore::JsonWriter& w, const char* k, const uint32_t* v, uint8_t n,
                       const DeviceState* only_in) {
    w.key(k);
    w.ch('[');
    bool first = true;
    for (uint8_t i = 0; i < n && i < 16; ++i) {
        if (v[i] == 0) continue;
        if (only_in != nullptr) {
            bool declared = false;
            for (uint8_t j = 0; j < only_in->n_denominations && j < 16; ++j) {
                if (only_in->denominations[j] == v[i]) declared = true;
            }
            if (!declared) continue;
        }
        if (!first) w.ch(',');
        first = false;
        w.num(static_cast<long>(v[i]));
    }
    w.ch(']');
}

void write_nullable_str(boardcore::JsonWriter& w, const char* k, const char* v) {
    w.raw(",");
    w.key(k);
    if (v[0] != '\0') {
        w.str(v);
    } else {
        w.raw("null");
    }
}

void write_device(boardcore::JsonWriter& w, const char* name, const DeviceState& d) {
    w.key(name);
    w.raw("{\"present\":");
    w.boolean(d.present);
    w.ch(',');
    w.field("status", status_str(d.status));
    w.raw(",\"level\":");
    if (d.level > 0) {
        w.num(d.level);
    } else {
        w.raw("null");
    }
    write_nullable_str(w, "model", d.model);
    write_nullable_str(w, "serial", d.serial);
    w.ch(',');
    write_cents_array(w, "denominations", d.denominations, d.n_denominations, nullptr);
    w.ch(',');
    write_cents_array(w, "enabled", d.enabled, d.n_enabled, &d);
    w.ch('}');
}

}  // namespace

size_t state_json(const PaymentState& s, const char* iso_ts, char* out, size_t cap) {
    boardcore::JsonWriter w(out, cap);
    w.ch('{');
    w.field("boot_id", s.boot_id != nullptr ? s.boot_id : "");
    w.raw(",\"seq_last\":");
    w.num(static_cast<long>(s.seq_last));
    w.raw(",\"currency\":\"USD\",\"policy_id\":");
    if (s.policy_id != nullptr && s.policy_id[0] != '\0') {
        w.str(s.policy_id);
    } else {
        w.raw("null");
    }
    w.raw(",\"accepting\":");
    w.boolean(s.inhibited == Inhibit::None);
    w.raw(",\"inhibited_reason\":");
    const char* why = inhibit_str(s.inhibited);
    if (why != nullptr) {
        w.str(why);
    } else {
        w.raw("null");
    }
    w.raw(",\"escrow_cents\":");
    w.num(static_cast<long>(s.escrow_cents));
    w.raw(",\"bill_in_escrow_cents\":");
    if (s.bill_in_escrow) {
        w.num(static_cast<long>(s.bill_in_escrow_cents));
    } else {
        w.raw("null");
    }
    w.raw(",\"devices\":{");
    write_device(w, "coin", s.devices[static_cast<int>(Device::Coin)]);
    w.ch(',');
    write_device(w, "bill", s.devices[static_cast<int>(Device::Bill)]);
    w.ch(',');
    write_device(w, "cashless", s.devices[static_cast<int>(Device::Cashless)]);
    w.raw("},\"tubes\":[");
    bool first = true;
    for (uint8_t i = 0; i < s.n_tubes && i < 16; ++i) {
        if (s.tubes[i].cents == 0) continue;
        if (!first) w.ch(',');
        first = false;
        w.raw("{\"cents\":");
        w.num(static_cast<long>(s.tubes[i].cents));
        w.raw(",\"count\":");
        w.num(static_cast<long>(s.tubes[i].count));
        w.raw(",\"full\":");
        w.boolean(s.tubes[i].full);
        w.ch('}');
    }
    w.raw("],\"exact_change\":");
    w.boolean(s.exact_change);
    w.raw(",\"stacker\":");
    if (s.stacker_known) {
        w.raw("{\"count\":");
        w.num(static_cast<long>(s.stacker_count));
        w.raw(",\"full\":");
        w.boolean(s.stacker_full);
        w.ch('}');
    } else {
        w.raw("null");
    }
    w.raw(",\"session\":");
    if (s.session_open) {
        w.raw("{\"session_id\":");
        w.str(s.session_id);
        w.raw(",\"funds_cents\":");
        if (s.funds_known) {
            w.num(static_cast<long>(s.funds_cents));
        } else {
            w.raw("null");
        }
        w.raw(",\"started_ts\":");
        w.str(s.session_started_ts != nullptr ? s.session_started_ts : iso_ts);
        w.ch('}');
    } else {
        w.raw("null");
    }
    w.ch(',');
    w.field("ts", iso_ts);
    w.ch('}');
    return w.done();
}

size_t topic_event(const WireCtx& ctx, char* out, size_t cap) {
    return topic_join(ctx, "payment/event", out, cap);
}
size_t topic_state(const WireCtx& ctx, char* out, size_t cap) {
    return topic_join(ctx, "payment/state", out, cap);
}
size_t topic_event_ack(const WireCtx& ctx, char* out, size_t cap) {
    return topic_join(ctx, "payment/event/ack", out, cap);
}
size_t topic_cmd(const WireCtx& ctx, char* out, size_t cap) {
    return topic_join(ctx, "cmd/mdb", out, cap);
}
size_t topic_ack(const WireCtx& ctx, char* out, size_t cap) {
    return topic_join(ctx, "cmd/mdb/ack", out, cap);
}

bool peek_boot_seq(const char* json, char* boot_id_out, uint32_t& seq_out) {
    const char* b = strstr(json, "\"boot_id\":\"");
    if (b == nullptr) return false;
    b += strlen("\"boot_id\":\"");
    size_t n = 0;
    while (b[n] != '\0' && b[n] != '"') {
        if (n >= 32) return false;
        boot_id_out[n] = b[n];
        ++n;
    }
    if (b[n] != '"' || n == 0) return false;
    boot_id_out[n] = '\0';
    const char* s = strstr(json, "\"seq\":");
    if (s == nullptr) return false;
    s += strlen("\"seq\":");
    if (*s < '0' || *s > '9') return false;
    uint64_t v = 0;
    while (*s >= '0' && *s <= '9') {
        v = v * 10 + static_cast<uint64_t>(*s - '0');
        if (v > 0xFFFFFFFFull) return false;
        ++s;
    }
    seq_out = static_cast<uint32_t>(v);
    return true;
}

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
