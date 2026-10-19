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
    // timeout_us is LINE-IDLE time, measured from the far end of the previous byte (or of the last
    // transmitted byte): it is not an arrival-time budget. A byte is delivered at the end of its stop
    // bit, one character time (about 1.15 ms at 9600 baud) after it started, so a real port must add
    // its own character time and margin (see rx_deadline_us in link.h) to timeout_us.
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
