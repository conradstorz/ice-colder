#include "command.h"

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

#include "wire.h"

namespace mdbcore {
namespace {

// ArduinoJson 7 has no StaticJsonDocument, so the document budget is a bump allocator over a
// fixed arena: nothing touches the heap, and the arena is reset on every decode call (the
// document never outlives it). Each block carries a size header so reallocate can copy.
class Arena : public ArduinoJson::Allocator {
public:
    void reset() {
        used_ = 0;
        last_ = kNone;
    }
    void* allocate(size_t size) override {
        size_t need = round(size);
        if (used_ + need > sizeof(buf_)) return nullptr;
        last_ = used_;
        used_ += need;
        memcpy(buf_ + last_, &size, sizeof(size));
        return buf_ + last_ + kHeader;
    }
    // Only the newest block can be returned; older ones wait for reset().
    void deallocate(void* ptr) override {
        if (ptr != nullptr && offset(ptr) == last_) {
            used_ = last_;
            last_ = kNone;
        }
    }
    void* reallocate(void* ptr, size_t new_size) override {
        if (ptr == nullptr) return allocate(new_size);
        if (offset(ptr) == last_) {  // newest block: resize in place
            size_t need = round(new_size);
            if (last_ + need > sizeof(buf_)) return nullptr;
            used_ = last_ + need;
            memcpy(buf_ + last_, &new_size, sizeof(new_size));
            return ptr;
        }
        size_t old;
        memcpy(&old, buf_ + offset(ptr), sizeof(old));
        void* n = allocate(new_size);
        if (n != nullptr) memcpy(n, ptr, old < new_size ? old : new_size);
        return n;
    }

private:
    static constexpr size_t kHeader = 8;
    static constexpr size_t kNone = static_cast<size_t>(-1);
    size_t round(size_t size) const { return kHeader + ((size + 7) & ~static_cast<size_t>(7)); }
    size_t offset(void* ptr) const {
        return static_cast<size_t>(static_cast<unsigned char*>(ptr) - buf_) - kHeader;
    }
    alignas(8) unsigned char buf_[4096];
    size_t used_ = 0;
    size_t last_ = kNone;
};

Arena g_arena;

void set_detail(Decoded& d, const char* msg) { snprintf(d.detail, sizeof(d.detail), "%s", msg); }

bool reject(Decoded& d, const char* field, const char* what) {
    d.result = Decode::Rejected;
    snprintf(d.detail, sizeof(d.detail), "%s %s", field, what);
    return false;
}

bool copy_str(char* dst, size_t cap, const char* src) {
    size_t n = strlen(src);
    if (n >= cap) return false;
    memcpy(dst, src, n + 1);
    return true;
}

// A real JSON integer (a float such as 100.0 or a string "100" is not one) inside [lo, hi].
bool read_uint(Decoded& d, JsonVariantConst v, const char* field, uint32_t lo, uint32_t hi,
               uint32_t& out) {
    if (!v.is<int64_t>()) return reject(d, field, "must be an integer");
    const int64_t x = v.as<int64_t>();
    if (x < static_cast<int64_t>(lo) || x > static_cast<int64_t>(hi)) {
        char range[40];
        snprintf(range, sizeof(range), "out of range [%lu, %lu]", static_cast<unsigned long>(lo),
                 static_cast<unsigned long>(hi));
        return reject(d, field, range);
    }
    out = static_cast<uint32_t>(x);
    return true;
}

bool read_bool(Decoded& d, JsonVariantConst v, const char* field, bool& out) {
    if (!v.is<bool>()) return reject(d, field, "must be a boolean");
    out = v.as<bool>();
    return true;
}

// Required string of 1..max chars (min_len overrides the lower bound).
bool read_str(Decoded& d, JsonVariantConst v, const char* field, size_t min_len, size_t max_len,
              char* dst, size_t cap) {
    if (!v.is<const char*>()) return reject(d, field, "must be a string");
    const char* s = v.as<const char*>();
    const size_t n = strlen(s);
    if (n < min_len || n > max_len || n >= cap) {
        char what[40];
        snprintf(what, sizeof(what), "must be %lu-%lu characters", static_cast<unsigned long>(min_len),
                 static_cast<unsigned long>(max_len));
        return reject(d, field, what);
    }
    memcpy(dst, s, n + 1);
    return true;
}

// Optional string or null; `present` reports whether a string was given.
bool read_opt_str(Decoded& d, JsonVariantConst v, const char* field, char* dst, size_t cap,
                  bool& present) {
    present = false;
    if (v.isNull()) return true;
    if (!v.is<const char*>()) return reject(d, field, "must be a string or null");
    if (!copy_str(dst, cap, v.as<const char*>())) return reject(d, field, "is too long");
    present = true;
    return true;
}

bool read_denoms(Decoded& d, JsonVariantConst v, const char* field, uint32_t* out, uint8_t& n) {
    if (!v.is<JsonArrayConst>()) return reject(d, field, "must be an array of integers");
    JsonArrayConst a = v.as<JsonArrayConst>();
    if (a.size() > 16) return reject(d, field, "has more than 16 entries");
    n = 0;
    for (JsonVariantConst item : a) {
        uint32_t cents;
        if (!read_uint(d, item, field, 1, 0xFFFFFFFFu, cents)) return false;
        out[n++] = cents;
    }
    return true;
}

bool decode_set_policy(JsonVariantConst p, Decoded& d) {
    Policy& pol = d.cmd.policy;
    uint32_t v;
    if (!read_str(d, p["policy_id"], "policy_id", 1, 64, pol.policy_id, sizeof(pol.policy_id))) return false;
    if (!read_bool(d, p["accept"], "accept", pol.accept)) return false;
    if (!read_uint(d, p["max_escrow_cents"], "max_escrow_cents", 1, 0xFFFFFFFFu, pol.max_escrow_cents)) return false;
    if (!read_denoms(d, p["coin_enabled"], "coin_enabled", pol.coin_enabled, pol.n_coin)) return false;
    if (!read_denoms(d, p["bill_enabled"], "bill_enabled", pol.bill_enabled, pol.n_bill)) return false;
    if (!read_bool(d, p["cashless_enabled"], "cashless_enabled", pol.cashless_enabled)) return false;
    if (!read_uint(d, p["change_threshold_cents"], "change_threshold_cents", 0, 0xFFFFFFFFu,
                   pol.change_threshold_cents)) return false;
    if (!read_uint(d, p["edge_timeout_s"], "edge_timeout_s", 5, 120, v)) return false;
    pol.edge_timeout_s = static_cast<uint16_t>(v);
    return read_bool(d, p["return_bill_on_lever"], "return_bill_on_lever", pol.return_bill_on_lever);
}

bool decode_payout(JsonVariantConst p, Decoded& d) {
    static const char* const kReasons[] = {"change",      "session_timeout", "cancel", "vend_failed",
                                           "maintenance", "admin",           "error",  "customer"};
    if (!read_uint(d, p["cents"], "cents", 1, 0xFFFFFFFFu, d.cmd.cents)) return false;
    JsonVariantConst r = p["reason"];
    if (!r.is<const char*>()) return reject(d, "reason", "must be a string");
    const char* rs = r.as<const char*>();
    bool known = false;
    for (const char* k : kReasons) {
        if (strcmp(k, rs) == 0) known = true;
    }
    if (!known) return reject(d, "reason", "is not a payout reason");
    copy_str(d.cmd.reason, sizeof(d.cmd.reason), rs);
    bool present;
    return read_opt_str(d, p["sale_id"], "sale_id", d.cmd.sale_id, sizeof(d.cmd.sale_id), present);
}

bool decode_vend_request(JsonVariantConst p, Decoded& d) {
    if (!read_uint(d, p["cents"], "cents", 1, 0xFFFFFFFFu, d.cmd.cents)) return false;
    if (!read_str(d, p["item"], "item", 1, 64, d.cmd.item, sizeof(d.cmd.item))) return false;
    return read_opt_str(d, p["session_id"], "session_id", d.cmd.session_id, sizeof(d.cmd.session_id),
                        d.cmd.has_session_id);
}

bool decode_vend_result(JsonVariantConst p, Decoded& d) {
    if (!read_str(d, p["session_id"], "session_id", 1, 64, d.cmd.session_id, sizeof(d.cmd.session_id))) return false;
    d.cmd.has_session_id = true;
    if (!read_str(d, p["request_id"], "request_id", 8, 64, d.cmd.vend_request_id,
                  sizeof(d.cmd.vend_request_id))) return false;
    JsonVariantConst o = p["outcome"];
    const char* os = o.is<const char*>() ? o.as<const char*>() : "";
    if (strcmp(os, "success") == 0) {
        d.cmd.success = true;
    } else if (strcmp(os, "failure") == 0) {
        d.cmd.success = false;
    } else {
        return reject(d, "outcome", "must be success or failure");
    }
    return true;
}

bool decode_end_session(JsonVariantConst p, Decoded& d) {
    if (!read_str(d, p["session_id"], "session_id", 1, 64, d.cmd.session_id, sizeof(d.cmd.session_id))) return false;
    d.cmd.has_session_id = true;
    return true;
}

bool decode_device_reset(JsonVariantConst p, Decoded& d) {
    JsonVariantConst v = p["device"];
    const char* s = v.is<const char*>() ? v.as<const char*>() : "";
    if (strcmp(s, "coin") == 0) d.cmd.device = Device::Coin;
    else if (strcmp(s, "bill") == 0) d.cmd.device = Device::Bill;
    else if (strcmp(s, "cashless") == 0) d.cmd.device = Device::Cashless;
    else return reject(d, "device", "must be coin, bill or cashless");
    return true;
}

bool decode_cash_sale(JsonVariantConst p, Decoded& d) {
    if (!read_uint(d, p["cents"], "cents", 1, 0xFFFFFFFFu, d.cmd.cents)) return false;
    bool present;
    return read_opt_str(d, p["sale_id"], "sale_id", d.cmd.sale_id, sizeof(d.cmd.sale_id), present);
}

bool digits(const char* s, int n, int& out) {
    out = 0;
    for (int i = 0; i < n; ++i) {
        if (s[i] < '0' || s[i] > '9') return false;
        out = out * 10 + (s[i] - '0');
    }
    return true;
}

// Days since 1970-01-01 of a proleptic Gregorian date (Howard Hinnant's algorithm).
int64_t days_from_civil(int y, int m, int d) {
    y -= m <= 2;
    const int64_t era = (y >= 0 ? y : y - 399) / 400;
    const int64_t yoe = y - era * 400;
    const int64_t doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

// "YYYY-MM-DDTHH:MM:SS" + optional ".fraction" + optional "Z" or "+/-HH:MM"; -1 when unparseable.
// Naive times (no designator) are taken as UTC.
int64_t parse_iso(const char* s) {
    if (strlen(s) < 19) return -1;
    int y, mo, da, h, mi, se;
    if (!digits(s, 4, y) || s[4] != '-' || !digits(s + 5, 2, mo) || s[7] != '-' ||
        !digits(s + 8, 2, da) || (s[10] != 'T' && s[10] != 't' && s[10] != ' ') ||
        !digits(s + 11, 2, h) || s[13] != ':' || !digits(s + 14, 2, mi) || s[16] != ':' ||
        !digits(s + 17, 2, se)) {
        return -1;
    }
    if (mo < 1 || mo > 12 || da < 1 || da > 31 || h > 23 || mi > 59 || se > 60) return -1;
    const char* t = s + 19;
    if (*t == '.') {
        ++t;
        if (*t < '0' || *t > '9') return -1;
        while (*t >= '0' && *t <= '9') ++t;
    }
    int64_t offset = 0;
    if (*t == 'Z' || *t == 'z') {
        ++t;
    } else if (*t == '+' || *t == '-') {
        int oh, om;
        if (!digits(t + 1, 2, oh) || t[3] != ':' || !digits(t + 4, 2, om)) return -1;
        offset = (oh * 3600 + om * 60) * (*t == '+' ? 1 : -1);
        t += 6;
    }
    if (*t != '\0') return -1;
    return days_from_civil(y, mo, da) * 86400 + h * 3600 + mi * 60 + se - offset;
}

}  // namespace

Decoded decode_command(const char* json, size_t len) {
    Decoded d;
    memset(&d, 0, sizeof(d));
    d.result = Decode::Rejected;
    d.cmd.kind = CmdKind::Unknown;
    d.cmd.timestamp_unix = -1;

    g_arena.reset();
    JsonDocument doc(&g_arena);
    DeserializationError err = deserializeJson(doc, json, len);
    if (err != DeserializationError::Ok) {
        d.result = Decode::NoRequestId;
        set_detail(d, "invalid JSON");
        return d;
    }
    if (!doc.is<JsonObject>()) {
        d.result = Decode::NoRequestId;
        set_detail(d, "payload root is not a JSON object");
        return d;
    }
    JsonVariantConst rid = doc["request_id"];
    if (!rid.is<const char*>()) {
        d.result = Decode::NoRequestId;
        set_detail(d, "request_id missing or not a string");
        return d;
    }
    const char* rs = rid.as<const char*>();
    const size_t rl = strlen(rs);
    if (rl < 8 || rl > 64) {
        d.result = Decode::NoRequestId;
        set_detail(d, "request_id must be 8-64 characters");
        return d;
    }
    copy_str(d.cmd.request_id, sizeof(d.cmd.request_id), rs);

    JsonVariantConst nm = doc["command"];
    const char* name = nm.is<const char*>() ? nm.as<const char*>() : "";
    if (!copy_str(d.cmd.name, sizeof(d.cmd.name), name)) d.cmd.name[0] = '\0';
    int ci = -1;
    for (int i = 0; i < 11; ++i) {
        if (d.cmd.name[0] != '\0' && strcmp(kCommands[i], d.cmd.name) == 0) {
            ci = i;
            break;
        }
    }
    if (ci < 0) {
        d.result = Decode::Unsupported;
        set_detail(d, "unsupported command");
        return d;
    }
    // kCommands order: ping, self_test, force_report, set_policy, payout, vend_request,
    // vend_result, end_session, return_escrow, device_reset, cash_sale.
    static const CmdKind kKinds[11] = {CmdKind::Ping,        CmdKind::SelfTest,
                                       CmdKind::ForceReport, CmdKind::SetPolicy,
                                       CmdKind::Payout,      CmdKind::VendRequest,
                                       CmdKind::VendResult,  CmdKind::EndSession,
                                       CmdKind::ReturnEscrow, CmdKind::DeviceReset,
                                       CmdKind::CashSale};
    d.cmd.kind = kKinds[ci];

    JsonVariantConst ts = doc["timestamp"];
    if (ts.is<const char*>()) d.cmd.timestamp_unix = parse_iso(ts.as<const char*>());

    JsonVariantConst params = doc["params"];
    bool ok = true;
    switch (d.cmd.kind) {
        case CmdKind::SetPolicy: ok = decode_set_policy(params, d); break;
        case CmdKind::Payout: ok = decode_payout(params, d); break;
        case CmdKind::VendRequest: ok = decode_vend_request(params, d); break;
        case CmdKind::VendResult: ok = decode_vend_result(params, d); break;
        case CmdKind::EndSession: ok = decode_end_session(params, d); break;
        case CmdKind::DeviceReset: ok = decode_device_reset(params, d); break;
        case CmdKind::CashSale: ok = decode_cash_sale(params, d); break;
        default: break;
    }
    if (!ok) return d;
    d.result = Decode::Ok;
    return d;
}

// payment/event/ack is decoded by a small hand parser, not ArduinoJson: it runs in the MQTT client
// task (no arena shared with decode_command, no blocking, no heap). It accepts one flat object whose
// values are strings, integers, true/false/null; only boot_id (string, 8-32 chars, no escapes) and
// seq (integer 1..2^32-1) are used, every other member is skipped. Anything nested is refused.
namespace {

struct Scan {
    const char* p;
    const char* end;
    void ws() {
        while (p < end && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) ++p;
    }
    bool eat(char c) {
        ws();
        if (p < end && *p == c) {
            ++p;
            return true;
        }
        return false;
    }
    // Reads a string; `out` receives up to cap-1 chars when non-null. `n` is the full length seen,
    // and `escaped` tells the caller a backslash was present (the fields we use never contain one).
    bool str(char* out, size_t cap, size_t& n, bool& escaped) {
        n = 0;
        escaped = false;
        if (!eat('"')) return false;
        while (p < end) {
            const char c = *p++;
            if (c == '"') {
                if (out != nullptr && cap > 0) out[n < cap ? n : cap - 1] = '\0';
                return true;
            }
            if (c == '\\') {
                escaped = true;
                if (p >= end) return false;
                ++p;
                ++n;
                continue;
            }
            if (out != nullptr && n + 1 < cap) out[n] = c;
            ++n;
        }
        return false;
    }
};

}  // namespace

bool decode_event_ack(const char* json, size_t len, char* boot_id, uint32_t& seq) {
    Scan s{json, json + len};
    if (!s.eat('{')) return false;
    char bid[34] = {};
    size_t bid_n = 0;
    bool have_bid = false, have_seq = false;
    int64_t seq_v = 0;
    for (bool first = true;; first = false) {
        if (s.eat('}')) break;
        if (!first && !s.eat(',')) return false;
        char key[12];
        size_t kn;
        bool kesc;
        if (!s.str(key, sizeof(key), kn, kesc) || kesc || !s.eat(':')) return false;
        const bool is_bid = kn == 7 && memcmp(key, "boot_id", 7) == 0;
        const bool is_seq = kn == 3 && memcmp(key, "seq", 3) == 0;
        s.ws();
        if (s.p >= s.end) return false;
        if (*s.p == '"') {
            size_t n;
            bool esc;
            if (!s.str(is_bid ? bid : nullptr, sizeof(bid), n, esc)) return false;
            if (is_seq) return false;  // a string where an integer is required
            if (is_bid) {
                if (esc || n > 32) return false;
                bid_n = n;
                have_bid = true;
            }
        } else if ((*s.p >= '0' && *s.p <= '9') || *s.p == '-') {
            const bool neg = *s.p == '-';
            if (neg) ++s.p;
            if (s.p >= s.end || *s.p < '0' || *s.p > '9') return false;
            int64_t v = 0;
            while (s.p < s.end && *s.p >= '0' && *s.p <= '9') {
                if (v < (int64_t{1} << 40)) v = v * 10 + (*s.p - '0');
                ++s.p;
            }
            if (s.p < s.end && (*s.p == '.' || *s.p == 'e' || *s.p == 'E')) return false;  // not an integer
            if (is_bid) return false;
            if (is_seq) {
                seq_v = neg ? -v : v;
                have_seq = true;
            }
        } else if (*s.p == 't' || *s.p == 'f' || *s.p == 'n') {
            const char* lit = *s.p == 't' ? "true" : *s.p == 'f' ? "false" : "null";
            const size_t ln = strlen(lit);
            if (static_cast<size_t>(s.end - s.p) < ln || memcmp(s.p, lit, ln) != 0) return false;
            s.p += ln;
            if (is_bid || is_seq) return false;
        } else {
            return false;  // nested object/array or garbage
        }
    }
    if (!have_bid || !have_seq || bid_n < 8 || seq_v < 1 || seq_v > 0xFFFFFFFFll) return false;
    memcpy(boot_id, bid, bid_n + 1);
    seq = static_cast<uint32_t>(seq_v);
    return true;
}

}  // namespace mdbcore
