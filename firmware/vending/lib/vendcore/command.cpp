#include "command.h"

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

#include "channels.h"

namespace vendcore {
namespace {

// ArduinoJson 7 has no StaticJsonDocument, so the 4096-byte document budget
// is a bump allocator over a fixed arena: nothing touches the heap, and the
// arena is reset on every decode_command call (the document never outlives
// it). Each block carries an 8-byte size header so reallocate can copy.
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
    // Header holds the block's requested size (for reallocate's copy).
    size_t round(size_t size) const { return kHeader + ((size + 7) & ~static_cast<size_t>(7)); }
    size_t offset(void* ptr) const { return static_cast<size_t>(static_cast<unsigned char*>(ptr) - buf_) - kHeader; }
    alignas(8) unsigned char buf_[4096];
    size_t used_ = 0;
    size_t last_ = kNone;
};

Arena g_arena;

void set_detail(Decoded& d, const char* msg) { snprintf(d.detail, sizeof(d.detail), "%s", msg); }

bool copy_str(char* dst, size_t cap, const char* src) {
    size_t n = strlen(src);
    if (n >= cap) return false;
    memcpy(dst, src, n + 1);
    return true;
}

void append(char* buf, size_t cap, const char* s) {
    if (buf[0] != '\0') strncat(buf, ", ", cap - strlen(buf) - 1);
    strncat(buf, s, cap - strlen(buf) - 1);
}

bool decode_dispense(JsonVariantConst params, Decoded& d) {
    JsonVariantConst slot = params["slot"];
    JsonVariantConst mech = params["mechanism"];
    JsonVariantConst prof = params["profile"];
    if (slot.isNull() || mech.isNull() || prof.isNull()) {
        char missing[32] = "";  // at most "slot, mechanism, profile"
        if (slot.isNull()) append(missing, sizeof(missing), "slot");
        if (mech.isNull()) append(missing, sizeof(missing), "mechanism");
        if (prof.isNull()) append(missing, sizeof(missing), "profile");
        d.result = Decode::Rejected;
        snprintf(d.detail, sizeof(d.detail), "dispense requires: slot, mechanism, profile (missing: %s)",
                 missing);
        return false;
    }
    if (!slot.is<int>() || slot.as<int>() < 0 || slot.as<int>() > 255) {
        d.result = Decode::Rejected;
        set_detail(d, "slot must be an integer >= 0");
        return false;
    }
    Mechanism m;
    const char* ms = mech.is<const char*>() ? mech.as<const char*>() : "";
    if (strcmp(ms, "bagged_ice") == 0) m = Mechanism::BaggedIce;
    else if (strcmp(ms, "water_fill") == 0) m = Mechanism::WaterFill;
    else {
        d.result = Decode::Rejected;
        set_detail(d, "mechanism must be bagged_ice or water_fill");
        return false;
    }
    ParseResult pr = parse_profile(prof, m, d.cmd.profile);
    if (!pr.ok) {
        d.result = Decode::Rejected;
        snprintf(d.detail, sizeof(d.detail), "%s", pr.detail);
        return false;
    }
    d.cmd.slot = static_cast<uint8_t>(slot.as<int>());
    d.cmd.mechanism = m;
    return true;
}

}  // namespace

Decoded decode_command(const char* json, size_t len) {
    Decoded d;
    memset(&d, 0, sizeof(d));
    d.result = Decode::Rejected;
    d.cmd.kind = CmdKind::Unknown;

    g_arena.reset();
    JsonDocument doc(&g_arena);
    DeserializationError err = deserializeJson(doc, json, len);
    if (err != DeserializationError::Ok || !doc.is<JsonObject>()) {
        set_detail(d, "invalid JSON");
        return d;
    }
    JsonVariantConst rid = doc["request_id"];
    if (!rid.is<const char*>()) {
        d.result = Decode::NoRequestId;
        set_detail(d, "request_id missing or not a string");
        return d;
    }
    const char* rs = rid.as<const char*>();
    size_t rl = strlen(rs);
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
    for (int i = 0; i < 5; ++i) {
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
    static const CmdKind kKinds[5] = {CmdKind::Ping, CmdKind::SelfTest, CmdKind::ForceReport,
                                      CmdKind::Dispense, CmdKind::WaterValve};
    d.cmd.kind = kKinds[ci];

    JsonVariantConst params = doc["params"];

    if (d.cmd.kind == CmdKind::WaterValve) {
        JsonVariantConst s = params["seconds"];
        if (!s.is<int>() || s.as<int>() < 1 || s.as<int>() > 10) {
            d.result = Decode::Rejected;
            set_detail(d, "seconds out of range [1, 10]");
            return d;
        }
        d.cmd.valve_seconds = static_cast<uint8_t>(s.as<int>());
    } else if (d.cmd.kind == CmdKind::Dispense) {
        if (!decode_dispense(params, d)) return d;
    }
    d.result = Decode::Ok;
    return d;
}

}  // namespace vendcore
