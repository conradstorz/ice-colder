// Small shared test doubles for vendcore tests.
#pragma once

#include <string.h>

#include "ports.h"

namespace vendcore {

struct FakeClock : Clock {
    uint32_t t = 0;
    uint32_t now_ms() const override { return t; }
};

struct FakeIo : IoPort {
    static constexpr int kMaxScript = 64;
    InputSnapshot script[kMaxScript] = {};
    int script_len = 0;
    int script_pos = 0;
    InputSnapshot current = {};
    bool read_ok = true;
    bool last_outputs[kOutputCount] = {};
    int write_count = 0;
    int kicks = 0;

    void push(const InputSnapshot& s) {
        if (script_len < kMaxScript) script[script_len++] = s;
    }
    bool read(InputSnapshot& out) override {
        if (!read_ok) return false;
        if (script_pos < script_len) current = script[script_pos++];
        out = current;
        return true;
    }
    bool write_outputs(const bool (&on)[kOutputCount]) override {
        memcpy(last_outputs, on, sizeof(last_outputs));
        ++write_count;
        return true;
    }
    void kick_watchdog() override { ++kicks; }
};

struct VecPublisher : Publisher {
    static constexpr int kCap = 256;
    Event events[kCap] = {};
    int count = 0;

    bool publish(const Event& e) override {
        if (count >= kCap) return false;
        events[count++] = e;
        return true;
    }
    void clear() { count = 0; }
};

}  // namespace vendcore
