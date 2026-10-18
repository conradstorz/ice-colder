// Allocation-free JSON appender into a caller buffer; any overflow poisons the
// whole result (done() then returns 0).
#pragma once

#include <stddef.h>
#include <stdio.h>
#include <string.h>

namespace boardcore {

struct JsonWriter {
    char* buf;
    size_t cap;
    size_t len = 0;
    bool bad = false;

    JsonWriter(char* b, size_t c) : buf(b), cap(c) {
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
    void field(const char* k, const char* v) {  // "k":"v"  (caller supplies the leading comma)
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

}  // namespace boardcore
