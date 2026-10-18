#include "wire.h"

#include <stdio.h>
#include <string.h>

#include "channels.h"
#include "dispenser.h"
#include "version.h"

namespace vendcore {
namespace {

// Appends into a caller buffer; any overflow poisons the whole result.
struct W {
    char* buf;
    size_t cap;
    size_t len = 0;
    bool bad = false;

    W(char* b, size_t c) : buf(b), cap(c) {
        if (cap > 0) buf[0] = '\0';
    }
    void raw(const char* s) {
        const size_t n = strlen(s);
        if (bad || len + n + 1 > cap) {
            bad = true;
            return;
        }
        memcpy(buf + len, s, n + 1);
        len += n;
    }
    void ch(char c) {
        const char s[2] = {c, '\0'};
        raw(s);
    }
    void str(const char* s) {  // quoted, escaped
        ch('"');
        for (; s != nullptr && *s != '\0'; ++s) {
            const unsigned char c = static_cast<unsigned char>(*s);
            if (c == '"' || c == '\\') {
                ch('\\');
                ch(static_cast<char>(c));
            } else if (c < 0x20) {
                ch(' ');
            } else {
                ch(static_cast<char>(c));
            }
        }
        ch('"');
    }
    void key(const char* k) {
        str(k);
        ch(':');
    }
    void field(const char* k, const char* v) {  // ,"k":"v"  (caller supplies the leading comma)
        key(k);
        str(v);
    }
    void num(long v) {
        char t[24];
        snprintf(t, sizeof(t), "%ld", v);
        raw(t);
    }
    void flt(float v) {
        char t[32];
        snprintf(t, sizeof(t), "%.2f", static_cast<double>(v));
        raw(t);
    }
    void boolean(bool b) { raw(b ? "true" : "false"); }
    size_t done() const { return bad ? 0 : len; }
};

// Closes the object, adding "timestamp" when the board has wall-clock time.
size_t close_obj(W& w, const WireCtx& ctx) {
    if (ctx.iso_time != nullptr) {
        w.ch(',');
        w.field("timestamp", ctx.iso_time);
    }
    w.ch('}');
    return w.done();
}

const char* ack_status(AckStatus s) {
    switch (s) {
        case AckStatus::Ok: return "ok";
        case AckStatus::Rejected: return "rejected";
        case AckStatus::Failed: return "failed";
        case AckStatus::Unsupported: return "unsupported";
    }
    return "failed";
}

bool is_button(int idx) {
    return idx >= static_cast<int>(In::ButtonIce) && idx <= static_cast<int>(In::ButtonWater5);
}

const char* kind_name(Kind k) {
    switch (k) {
        case Kind::Binary: return "binary";
        case Kind::Counter: return "counter";
        case Kind::Temperature: return "temperature";
    }
    return "binary";
}

// Words of an id, capitalised ("sump_high" -> "Sump High").
void title(W& w, const char* id) {
    bool up = true;
    for (; *id != '\0'; ++id) {
        char c = *id;
        if (c == '_') {
            w.ch(' ');
            up = true;
        } else {
            if (up && c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
            w.ch(c);
            up = false;
        }
    }
}

// HA entities: every binary channel except customer buttons, every temperature
// channel, then uptime (mirrors ice_sims/vending/ha.py). n-th -> kChannels index, -1 past the end.
int entity_channel(int n) {
    int seen = 0;
    for (int i = 0; i < kChannelCount; ++i) {
        const Channel& c = kChannels[i];
        const bool ok = (c.kind == Kind::Binary && !is_button(i)) || c.kind == Kind::Temperature;
        if (!ok) continue;
        if (seen == n) return i;
        ++seen;
    }
    return -1;
}

}  // namespace

size_t topic_join(const WireCtx& ctx, const char* tail, char* out, size_t cap) {
    const int n = snprintf(out, cap, "vmc/%s/%s", ctx.machine_id, tail);
    return (n > 0 && static_cast<size_t>(n) < cap) ? static_cast<size_t>(n) : 0;
}

size_t topic_for(const Event& e, const WireCtx& ctx, char* out, size_t cap) {
    const bool by_channel = e.type == EventType::InputChanged || e.type == EventType::OutputChanged ||
                            e.type == EventType::Telemetry || e.type == EventType::Temperature;
    if (by_channel && e.channel >= kChannelCount) return 0;
    char tail[96];
    switch (e.type) {
        case EventType::InputChanged:
        case EventType::OutputChanged: snprintf(tail, sizeof(tail), "hardware/io/%s", kChannels[e.channel].id); break;
        case EventType::ButtonPressed: snprintf(tail, sizeof(tail), "hardware/buttons"); break;
        case EventType::Step:
        case EventType::Outcome: snprintf(tail, sizeof(tail), "hardware/dispenser"); break;
        case EventType::Telemetry:
            snprintf(tail, sizeof(tail), "telemetry/%s/%s", kSubsystem, kChannels[e.channel].id);
            break;
        case EventType::Temperature: snprintf(tail, sizeof(tail), "sensors/temp/%s", kChannels[e.channel].id); break;
        case EventType::Health: return 0;
    }
    return topic_join(ctx, tail, out, cap);
}

size_t payload_for(const Event& e, const WireCtx& ctx, uint32_t, char* out, size_t cap) {
    W w(out, cap);
    w.ch('{');
    const bool by_channel = e.type != EventType::Step && e.type != EventType::Outcome;
    if (by_channel && e.channel >= kChannelCount) return 0;
    switch (e.type) {
        case EventType::InputChanged:
        case EventType::OutputChanged:
            w.field("device", kChannels[e.channel].id);
            w.ch(',');
            w.key("state");
            w.boolean(e.state);
            break;
        case EventType::ButtonPressed:
            w.field("button", kChannels[e.channel].id);
            w.ch(',');
            w.field("action", "pressed");
            break;
        case EventType::Step:
        case EventType::Outcome: {
            const bool outcome = e.type == EventType::Outcome;
            w.key("slot");
            w.num(e.slot);
            w.ch(',');
            w.field("state", outcome ? outcome_name(static_cast<Outcome>(e.value)) : e.text);
            w.ch(',');
            w.field("request_id", e.request_id);
            if (outcome && e.text[0] != '\0') {
                w.ch(',');
                w.field("detail", e.text);
            }
            break;
        }
        case EventType::Telemetry:
            w.field("channel_id", kChannels[e.channel].id);
            w.ch(',');
            w.key("value");
            w.num(static_cast<long>(e.value));
            break;
        case EventType::Temperature:
            w.field("location", kChannels[e.channel].id);
            w.ch(',');
            w.key("value");
            w.flt(e.fvalue);
            w.ch(',');
            w.field("unit", kChannels[e.channel].unit);
            break;
        case EventType::Health: return 0;
    }
    return close_obj(w, ctx);
}

size_t capabilities_json(const WireCtx& ctx, char* out, size_t cap) {
    W w(out, cap);
    w.ch('{');
    w.field("subsystem", kSubsystem);
    w.ch(',');
    w.field("firmware", ctx.firmware);
    w.ch(',');
    w.field("contract_version", kContractVersion);
    w.ch(',');
    w.field("brand", "Kooler Ice");
    w.ch(',');
    w.field("model", "IM600XL");
    w.ch(',');
    w.field("hardware_id", ctx.hardware_id);
    w.ch(',');
    w.field("ip", ctx.ip);
    w.ch(',');
    w.key("channels");
    w.ch('[');
    for (int i = 0; i < kChannelCount; ++i) {
        const Channel& c = kChannels[i];
        if (i > 0) w.ch(',');
        w.ch('{');
        w.field("channel_id", c.id);
        w.ch(',');
        w.field("kind", kind_name(c.kind));
        w.ch(',');
        w.field("unit", c.unit);
        w.ch(',');
        w.field("description", c.description);
        w.ch(',');
        w.key("interval_seconds");
        w.flt(c.interval_s);
        w.ch(',');
        w.field("direction", c.dir == Dir::Output ? "output" : "input");
        w.ch(',');
        w.key("driven_by");
        if (c.driven_by != nullptr) w.str(c.driven_by); else w.raw("null");
        w.ch('}');
    }
    w.ch(']');
    w.ch(',');
    w.key("commands");
    w.ch('[');
    for (int i = 0; i < 5; ++i) {
        if (i > 0) w.ch(',');
        w.str(kCommands[i]);
    }
    w.ch(']');
    return close_obj(w, ctx);
}

size_t heartbeat_json(uint32_t uptime_s, char* out, size_t cap) {
    W w(out, cap);
    w.ch('{');
    w.field("subsystem", kSubsystem);
    w.ch(',');
    w.key("uptime_seconds");
    w.num(static_cast<long>(uptime_s));
    w.ch('}');
    return w.done();
}

size_t lwt_json(char* out, size_t cap) {
    W w(out, cap);
    w.ch('{');
    w.field("subsystem", kSubsystem);
    w.ch(',');
    w.key("uptime_seconds");
    w.num(-1);
    w.ch('}');
    return w.done();
}

size_t ack_json(const Ack& a, char* out, size_t cap) {
    W w(out, cap);
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

int ha_discovery_count() {
    int n = 0;
    while (entity_channel(n) >= 0) ++n;
    return n + 1;  // + uptime
}

size_t ha_discovery(int i, const WireCtx& ctx, char* topic, size_t tcap, char* payload, size_t pcap) {
    if (i < 0 || i >= ha_discovery_count()) return 0;
    const int idx = entity_channel(i);
    const bool uptime = idx < 0;
    const Channel* c = uptime ? nullptr : &kChannels[idx];
    const bool temp = c != nullptr && c->kind == Kind::Temperature;
    const char* component = (uptime || temp) ? "sensor" : "binary_sensor";
    const char* object_id = uptime ? "uptime" : c->id;

    char node[80];
    snprintf(node, sizeof(node), "%s_%s", ctx.machine_id, kSubsystem);
    const int tn = snprintf(topic, tcap, "homeassistant/%s/%s/%s/config", component, node, object_id);
    if (tn <= 0 || static_cast<size_t>(tn) >= tcap) return 0;

    char name[80];
    {
        W t(name, sizeof(name));
        t.raw("Vending ");
        if (uptime) t.raw("Machine Uptime"); else title(t, c->id);
        if (temp) t.raw(" Temperature");
    }
    char uid[112];
    snprintf(uid, sizeof(uid), "%s_%s", node, object_id);
    char state_topic[128];
    if (uptime) snprintf(state_topic, sizeof(state_topic), "vmc/%s/heartbeat/%s", ctx.machine_id, kSubsystem);
    else if (temp) snprintf(state_topic, sizeof(state_topic), "vmc/%s/sensors/temp/%s", ctx.machine_id, c->id);
    else snprintf(state_topic, sizeof(state_topic), "vmc/%s/hardware/io/%s", ctx.machine_id, c->id);
    char dev_name[80];
    snprintf(dev_name, sizeof(dev_name), "%s Vending", ctx.machine_id);

    W w(payload, pcap);
    w.ch('{');
    w.field("name", name);
    w.ch(',');
    w.field("unique_id", uid);
    w.ch(',');
    w.field("state_topic", state_topic);
    w.ch(',');
    w.field("value_template", uptime ? "{{ value_json.uptime_seconds }}"
                                     : temp ? "{{ value_json.value }}" : "{{ 'ON' if value_json.state else 'OFF' }}");
    w.ch(',');
    w.key("device");
    w.ch('{');
    w.key("identifiers");
    w.ch('[');
    w.str(node);
    w.ch(']');
    w.ch(',');
    w.field("name", dev_name);
    w.ch(',');
    w.field("manufacturer", "ice-colder");
    w.ch(',');
    w.field("model", "ESP32 vending");
    w.ch(',');
    w.field("via_device", ctx.machine_id);
    w.ch('}');
    if (uptime) {
        w.raw(",\"device_class\":\"duration\",\"unit_of_measurement\":\"s\",\"state_class\":\"total_increasing\"");
    } else if (temp) {
        w.raw(",\"device_class\":\"temperature\",\"unit_of_measurement\":\"\xC2\xB0"
              "C\",\"state_class\":\"measurement\",\"expire_after\":30");
    } else {
        const char* dc = c->dir == Dir::Output ? "running" : "occupancy";
        if (idx == static_cast<int>(In::SumpHigh)) dc = "moisture";
        if (idx == static_cast<int>(In::ServiceDoor)) dc = "door";
        w.ch(',');
        w.field("device_class", dc);
        w.raw(",\"payload_on\":\"ON\",\"payload_off\":\"OFF\"");
    }
    w.ch('}');
    return w.done();
}

}  // namespace vendcore
