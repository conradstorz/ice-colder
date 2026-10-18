#include "profile.h"

#include <stdio.h>
#include <string.h>

#include "channels.h"

namespace vendcore {
namespace {

constexpr float kInf = 1e30f;

bool fail(ParseResult& r, const char* fmt, const char* a, const char* b = "") {
    r.ok = false;
    snprintf(r.detail, sizeof(r.detail), fmt, a, b);
    return false;
}

// Joins "<path>.<key>" into buf; path may be empty.
const char* join(char* buf, size_t n, const char* path, const char* key) {
    if (path[0] == '\0') snprintf(buf, n, "%s", key);
    else snprintf(buf, n, "%s.%s", path, key);
    return buf;
}

template <size_t N>
bool check_keys(JsonObjectConst o, const char* const (&allowed)[N], ParseResult& r) {
    for (JsonPairConst kv : o) {
        bool known = false;
        for (const char* a : allowed) {
            if (strcmp(kv.key().c_str(), a) == 0) { known = true; break; }
        }
        if (!known) return fail(r, "unknown key %s", kv.key().c_str());
    }
    return true;
}

bool get_obj(JsonObjectConst parent, const char* key, JsonObjectConst& out, ParseResult& r) {
    JsonVariantConst v = parent[key];
    if (v.isNull()) return fail(r, "%s missing", key);
    if (!v.is<JsonObjectConst>()) return fail(r, "%s invalid", key);
    out = v.as<JsonObjectConst>();
    return true;
}

bool get_num(JsonObjectConst o, const char* key, const char* path, float lo, bool lo_excl, float hi,
             float& out, ParseResult& r) {
    char buf[64];
    JsonVariantConst v = o[key];
    if (v.isNull()) return fail(r, "%s missing", join(buf, sizeof(buf), path, key));
    if (!v.is<float>()) return fail(r, "%s invalid", join(buf, sizeof(buf), path, key));
    float x = v.as<float>();
    if ((lo_excl ? x <= lo : x < lo) || x > hi)
        return fail(r, "%s out of range", join(buf, sizeof(buf), path, key));
    out = x;
    return true;
}

bool get_str(JsonObjectConst o, const char* key, const char* path, const char*& out, ParseResult& r) {
    char buf[64];
    JsonVariantConst v = o[key];
    if (v.isNull()) return fail(r, "%s missing", join(buf, sizeof(buf), path, key));
    if (!v.is<const char*>()) return fail(r, "%s invalid", join(buf, sizeof(buf), path, key));
    out = v.as<const char*>();
    return true;
}

bool get_proof(JsonObjectConst o, const char* path, const char* const* names, int n, int& which,
               ParseResult& r) {
    const char* s;
    if (!get_str(o, "proof", path, s, r)) return false;
    for (int i = 0; i < n; ++i) {
        if (strcmp(s, names[i]) == 0) { which = i; return true; }
    }
    char buf[64];
    return fail(r, "%s invalid", join(buf, sizeof(buf), path, "proof"));
}

enum class Want { Output, Input };

bool resolve_channel(const char* id, const char* field, const char* path, Want want, int& idx,
                     ParseResult& r) {
    char buf[64];
    idx = channel_index(id);
    if (idx < 0) return fail(r, "undeclared channel %s", id);
    bool is_out = kChannels[idx].dir == Dir::Output;
    if (is_out != (want == Want::Output))
        return fail(r, "%s wrong direction", join(buf, sizeof(buf), path, field));
    return true;
}

bool get_chan(JsonObjectConst o, const char* key, const char* path, Want want, int& idx,
              ParseResult& r) {
    const char* id;
    if (!get_str(o, key, path, id, r)) return false;
    return resolve_channel(id, key, path, want, idx, r);
}

// stall_current_amps / current_channel: both set or both "unmonitored".
bool check_current(JsonObjectConst o, const char* path, ParseResult& r) {
    char buf[64];
    JsonVariantConst a = o["stall_current_amps"];
    if (a.isNull()) return fail(r, "%s missing", join(buf, sizeof(buf), path, "stall_current_amps"));
    bool a_set;
    if (a.is<const char*>()) {
        if (strcmp(a.as<const char*>(), "unmonitored") != 0)
            return fail(r, "%s invalid", join(buf, sizeof(buf), path, "stall_current_amps"));
        a_set = false;
    } else {
        float x;
        if (!get_num(o, "stall_current_amps", path, 0.1f, false, 50.0f, x, r)) return false;
        a_set = true;
    }
    const char* cid;
    if (!get_str(o, "current_channel", path, cid, r)) return false;
    bool c_set = strcmp(cid, "unmonitored") != 0;
    if (c_set) {
        int idx;
        if (!resolve_channel(cid, "current_channel", path, Want::Input, idx, r)) return false;
    }
    if (a_set != c_set) return fail(r, "%s invalid", join(buf, sizeof(buf), path, "current_channel"));
    return true;
}

bool parse_agitate(JsonObjectConst root, SlotProfile& p, ParseResult& r) {
    static const char* const kKeys[] = {"motor_channel", "run_seconds", "stall_current_amps",
                                        "current_channel"};
    JsonObjectConst o;
    if (!get_obj(root, "agitate", o, r) || !check_keys(o, kKeys, r)) return false;
    return get_chan(o, "motor_channel", "agitate", Want::Output, p.agitate.motor, r) &&
           get_num(o, "run_seconds", "agitate", 0.5f, false, 60.0f, p.agitate.run_s, r) &&
           check_current(o, "agitate", r);
}

bool parse_ice_fill(JsonObjectConst root, SlotProfile& p, ParseResult& r) {
    static const char* const kSensor[] = {"proof", "motor_channel", "sensor_channel", "max_run_seconds",
                                          "stall_current_amps", "current_channel"};
    static const char* const kTimed[] = {"proof", "motor_channel", "max_run_seconds",
                                         "stall_current_amps", "current_channel"};
    static const char* const kProofs[] = {"bag_full_sensor", "timed"};
    JsonObjectConst o;
    int which;
    if (!get_obj(root, "fill", o, r) || !get_proof(o, "fill", kProofs, 2, which, r)) return false;
    p.ice.proof = which == 0 ? IceFill::BySensor : IceFill::Timed;
    bool ok = which == 0 ? check_keys(o, kSensor, r) : check_keys(o, kTimed, r);
    if (!ok) return false;
    if (!get_chan(o, "motor_channel", "fill", Want::Output, p.ice.motor, r)) return false;
    if (which == 0 && !get_chan(o, "sensor_channel", "fill", Want::Input, p.ice.sensor, r)) return false;
    return get_num(o, "max_run_seconds", "fill", 1.0f, false, 120.0f, p.ice.max_run_s, r) &&
           check_current(o, "fill", r);
}

bool parse_release(JsonObjectConst root, SlotProfile& p, ParseResult& r) {
    static const char* const kCatch[] = {"proof", "catch_channel", "drop_seconds"};
    static const char* const kTimed[] = {"proof", "solenoid_channel", "pulse_seconds"};
    static const char* const kSensor[] = {"proof", "solenoid_channel", "sensor_channel", "pulse_seconds",
                                          "open_timeout_seconds", "close_timeout_seconds"};
    static const char* const kProofs[] = {"catch", "timed", "door_sensor"};
    JsonObjectConst o;
    int which;
    if (!get_obj(root, "release", o, r) || !get_proof(o, "release", kProofs, 3, which, r)) return false;
    Release& rel = p.release;
    if (which == 0) {
        rel.proof = Release::ByCatch;
        return check_keys(o, kCatch, r) &&
               get_chan(o, "catch_channel", "release", Want::Output, rel.channel, r) &&
               get_num(o, "drop_seconds", "release", 0.5f, false, 30.0f, rel.drop_s, r);
    }
    rel.proof = which == 1 ? Release::Timed : Release::BySensor;
    bool ok = which == 1 ? check_keys(o, kTimed, r) : check_keys(o, kSensor, r);
    if (!ok || !get_chan(o, "solenoid_channel", "release", Want::Output, rel.channel, r)) return false;
    if (which == 2 && !get_chan(o, "sensor_channel", "release", Want::Input, rel.sensor, r)) return false;
    if (!get_num(o, "pulse_seconds", "release", 0.1f, false, 10.0f, rel.pulse_s, r)) return false;
    if (which == 2)
        return get_num(o, "open_timeout_seconds", "release", 0.5f, false, 30.0f, rel.open_timeout_s, r) &&
               get_num(o, "close_timeout_seconds", "release", 0.5f, false, 60.0f, rel.close_timeout_s, r);
    return true;
}

bool parse_water_fill(JsonObjectConst root, SlotProfile& p, ParseResult& r) {
    static const char* const kVolume[] = {"proof", "valve_channel", "flow_sensor_channel",
                                          "target_volume_ml", "pulses_per_liter",
                                          "min_flow_ml_per_second", "no_flow_grace_seconds",
                                          "over_dispense_percent", "max_fill_seconds"};
    static const char* const kTimed[] = {"proof", "valve_channel", "max_fill_seconds"};
    static const char* const kProofs[] = {"flow_volume", "timed"};
    JsonObjectConst o;
    int which;
    if (!get_obj(root, "fill", o, r) || !get_proof(o, "fill", kProofs, 2, which, r)) return false;
    WaterFill& w = p.water;
    w.proof = which == 0 ? WaterFill::ByVolume : WaterFill::Timed;
    bool ok = which == 0 ? check_keys(o, kVolume, r) : check_keys(o, kTimed, r);
    if (!ok || !get_chan(o, "valve_channel", "fill", Want::Output, w.valve, r)) return false;
    if (which == 0) {
        if (!get_chan(o, "flow_sensor_channel", "fill", Want::Input, w.flow, r) ||
            !get_num(o, "target_volume_ml", "fill", 50.0f, false, 50000.0f, w.target_ml, r) ||
            !get_num(o, "pulses_per_liter", "fill", 0.0f, true, kInf, w.pulses_per_l, r) ||
            !get_num(o, "min_flow_ml_per_second", "fill", 0.0f, true, kInf, w.min_flow_ml_s, r) ||
            !get_num(o, "no_flow_grace_seconds", "fill", 0.5f, false, 30.0f, w.no_flow_grace_s, r) ||
            !get_num(o, "over_dispense_percent", "fill", 0.0f, false, 50.0f, w.over_pct, r))
            return false;
    }
    return get_num(o, "max_fill_seconds", "fill", 1.0f, false, 600.0f, w.max_fill_s, r);
}

// Step bit for a name valid for this mechanism, or 0.
uint8_t step_bit(const char* s, Mechanism m) {
    if (m == Mechanism::BaggedIce) {
        if (strcmp(s, "agitate") == 0) return 1;
        if (strcmp(s, "release") == 0) return 4;
    }
    if (strcmp(s, "fill") == 0) return 2;
    return 0;
}

bool parse_accessories(JsonObjectConst root, SlotProfile& p, ParseResult& r) {
    static const char* const kKeys[] = {"channel", "on_during", "lead_seconds", "lag_seconds"};
    JsonVariantConst av = root["accessories"];
    if (av.isNull()) return true;
    char path[64];
    if (!av.is<JsonObjectConst>()) return fail(r, "%s invalid", "accessories");
    for (JsonPairConst kv : av.as<JsonObjectConst>()) {
        const char* name = kv.key().c_str();
        snprintf(path, sizeof(path), "accessories.%s", name);
        if (p.acc_count >= 4) return fail(r, "%s out of range", "accessories");
        if (!kv.value().is<JsonObjectConst>()) return fail(r, "%s invalid", path);
        JsonObjectConst o = kv.value().as<JsonObjectConst>();
        Accessory& a = p.acc[p.acc_count];
        if (!check_keys(o, kKeys, r) || !get_chan(o, "channel", path, Want::Output, a.channel, r) ||
            !get_num(o, "lead_seconds", path, 0.0f, false, 30.0f, a.lead_s, r) ||
            !get_num(o, "lag_seconds", path, 0.0f, false, 30.0f, a.lag_s, r))
            return false;
        JsonVariantConst od = o["on_during"];
        if (od.isNull()) return fail(r, "%s.on_during missing", path);
        if (!od.is<JsonArrayConst>() || od.as<JsonArrayConst>().size() == 0)
            return fail(r, "%s.on_during invalid", path);
        uint8_t mask = 0;
        bool all = false;
        size_t count = 0;
        for (JsonVariantConst e : od.as<JsonArrayConst>()) {
            ++count;
            if (!e.is<const char*>()) return fail(r, "%s.on_during invalid", path);
            const char* s = e.as<const char*>();
            if (strcmp(s, "all") == 0) { all = true; continue; }
            uint8_t bit = step_bit(s, p.mechanism);
            if (bit == 0 || (mask & bit)) return fail(r, "%s.on_during invalid", path);
            mask |= bit;
        }
        if (all) {
            if (count != 1) return fail(r, "%s.on_during invalid", path);
            mask = 7;
        } else {
            // Named steps must be consecutive: the set bits form one run.
            uint8_t low = mask & static_cast<uint8_t>(-mask);
            uint8_t carried = static_cast<uint8_t>(mask + low);
            if (carried & mask) return fail(r, "%s.on_during invalid", path);
        }
        a.on_during_mask = mask;
        ++p.acc_count;
    }
    return true;
}

}  // namespace

ParseResult parse_profile(JsonVariantConst v, Mechanism expected, SlotProfile& out) {
    static const char* const kIceKeys[] = {"mechanism", "product_sku", "agitate", "fill", "release",
                                           "accessories"};
    static const char* const kWaterKeys[] = {"mechanism", "product_sku", "fill", "accessories"};
    out = SlotProfile{};
    out.agitate.motor = -1;
    out.ice.sensor = -1;
    out.ice.motor = -1;
    out.release.channel = -1;
    out.release.sensor = -1;
    out.water.valve = -1;
    out.water.flow = -1;
    for (Accessory& a : out.acc) a.channel = -1;
    ParseResult r{true, {0}};
    if (!v.is<JsonObjectConst>()) {
        fail(r, "profile invalid", "");
        return r;
    }
    JsonObjectConst root = v.as<JsonObjectConst>();

    const char* mech;
    if (!get_str(root, "mechanism", "", mech, r)) return r;
    const char* want = expected == Mechanism::BaggedIce ? "bagged_ice" : "water_fill";
    if (strcmp(mech, want) != 0) {
        fail(r, "mechanism mismatch", "");
        return r;
    }
    out.mechanism = expected;

    bool ok = expected == Mechanism::BaggedIce ? check_keys(root, kIceKeys, r)
                                               : check_keys(root, kWaterKeys, r);
    if (!ok) return r;

    const char* sku;
    if (!get_str(root, "product_sku", "", sku, r)) return r;
    if (sku[0] == '\0' || strlen(sku) >= sizeof(out.sku)) {
        fail(r, "product_sku out of range", "");
        return r;
    }
    strcpy(out.sku, sku);

    if (expected == Mechanism::BaggedIce) {
        ok = parse_agitate(root, out, r) && parse_ice_fill(root, out, r) && parse_release(root, out, r);
    } else {
        ok = parse_water_fill(root, out, r);
    }
    if (ok) ok = parse_accessories(root, out, r);
    return r;
}

float worst_case_seconds(const SlotProfile& p) {
    float total;
    if (p.mechanism == Mechanism::BaggedIce) {
        total = p.agitate.run_s + p.ice.max_run_s;
        total += p.release.proof == Release::ByCatch ? p.release.drop_s : p.release.pulse_s;
        if (p.release.proof == Release::BySensor)
            total += p.release.open_timeout_s + p.release.close_timeout_s;
    } else {
        total = p.water.max_fill_s;
        if (p.water.proof == WaterFill::ByVolume) total += p.water.no_flow_grace_s;
    }
    float lead = 0.0f, lag = 0.0f;
    for (uint8_t i = 0; i < p.acc_count; ++i) {
        if (p.acc[i].lead_s > lead) lead = p.acc[i].lead_s;
        if (p.acc[i].lag_s > lag) lag = p.acc[i].lag_s;
    }
    return total + lead + lag;
}

}  // namespace vendcore
