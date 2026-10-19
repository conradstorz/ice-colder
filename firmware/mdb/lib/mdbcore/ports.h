// Hardware and storage seams; implemented by the ESP-IDF layer on target, by fakes on the host.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace mdbcore {

struct Clock {
    virtual uint32_t now_ms() const = 0;
    virtual uint64_t now_us() const = 0;
};

struct RxByte {
    uint8_t data;
    bool mode;
    uint64_t t_us;
};

struct BusPort {
    virtual void write(const uint8_t* bytes, const bool* modes, uint8_t n) = 0;
    virtual bool read(RxByte& out, uint32_t timeout_us) = 0;
    virtual void flush_rx() = 0;
};

struct IdSource {
    virtual void fill_hex(char* out, size_t n_chars) = 0;
};

// Entries are kept in append order; index 0 is the oldest.
struct LogStore {
    static constexpr size_t kMaxJson = 512;
    virtual bool append(const char* json, size_t len) = 0;
    virtual int count() const = 0;
    virtual bool get(int index, char* out, size_t cap) const = 0;
    virtual void erase_first(int n) = 0;
};

}  // namespace mdbcore
