// Host-test fakes for the mdbcore ports. Fixed arrays only, no heap.
#pragma once

#include <stdio.h>
#include <string.h>

#include "link.h"
#include "ports.h"

namespace mdbcore {

struct FakeClock : Clock {
    uint64_t us = 0;
    uint32_t now_ms() const override { return static_cast<uint32_t>(us / 1000); }
    uint64_t now_us() const override { return us; }
    void advance_us(uint64_t d) { us += d; }
    void advance_ms(uint32_t d) { us += static_cast<uint64_t>(d) * 1000; }
};

struct FakeIds : IdSource {
    uint32_t counter = 0;
    void fill_hex(char* out, size_t n_chars) override {
        static const char hex[] = "0123456789abcdef";
        ++counter;
        for (size_t i = 0; i < n_chars; ++i) {
            // Deterministic: counter in the low nibbles, zero padded on the left.
            const size_t from_right = n_chars - 1 - i;
            out[i] = from_right < 8 ? hex[(counter >> (4 * from_right)) & 0xF] : '0';
        }
    }
};

struct FakeLogStore : LogStore {
    static constexpr int kSlots = 300;
    char items[kSlots][kMaxJson];
    int n = 0;
    bool append(const char* json, size_t len) override {
        if (n >= kSlots || len >= kMaxJson) return false;
        memcpy(items[n], json, len);
        items[n][len] = '\0';
        ++n;
        return true;
    }
    int count() const override { return n; }
    bool get(int index, char* out, size_t cap) const override {
        if (index < 0 || index >= n) return false;
        const size_t len = strlen(items[index]);
        if (len + 1 > cap) return false;
        memcpy(out, items[index], len + 1);
        return true;
    }
    void erase_first(int k) override {
        if (k <= 0) return;
        if (k > n) k = n;
        for (int i = k; i < n; ++i) memcpy(items[i - k], items[i], kMaxJson);
        n -= k;
    }
};

struct FakePeripheral {
    virtual Response respond(uint8_t cmd, const uint8_t* data, uint8_t n) = 0;  // cmd = low 3 bits
    virtual void on_master_byte(uint8_t) {}
};

class FakeBus : public BusPort {
public:
    static constexpr int kMaxPeripherals = 3;
    static constexpr int kTranscript = 512;

    explicit FakeBus(FakeClock& clock) : clock_(clock) {}

    uint32_t gap_us = 300;
    bool drop_checksum_mode_bit = false;
    bool corrupt_next = false;         // flip a bit in the next reply block's checksum
    bool corrupt_retransmit = false;   // also corrupt the block resent after a RET
    int silent_next = 0;               // reply blocks to swallow

    uint8_t tx_bytes[kTranscript];
    bool tx_modes[kTranscript];
    int tx_len = 0;

    void attach(uint8_t address, FakePeripheral* p) {
        for (int i = 0; i < kMaxPeripherals; ++i) {
            if (!peers_[i]) {
                peers_[i] = p;
                addrs_[i] = address;
                return;
            }
        }
    }

    void write(const uint8_t* bytes, const bool* modes, uint8_t n) override {
        for (uint8_t i = 0; i < n; ++i) {
            if (tx_len < kTranscript) {
                tx_bytes[tx_len] = bytes[i];
                tx_modes[tx_len++] = modes[i];
            }
        }
        if (n == 1 && !modes[0]) {  // master ACK / RET / NAK
            if (bytes[0] == kRet && last_valid_) queue_block(last_reply_, true);
            if (last_peer_) last_peer_->on_master_byte(bytes[0]);
            return;
        }
        if (n < 2 || !modes[0]) return;
        if (checksum(bytes, static_cast<uint8_t>(n - 1)) != bytes[n - 1]) return;
        FakePeripheral* peer = nullptr;
        for (int i = 0; i < kMaxPeripherals; ++i) {
            if (peers_[i] && addrs_[i] == (bytes[0] & 0xF8)) peer = peers_[i];
        }
        if (!peer) return;
        last_peer_ = peer;
        Response r = peer->respond(bytes[0] & 0x07, bytes + 1, static_cast<uint8_t>(n - 2));
        if (r.kind == Reply::Timeout) return;
        if (silent_next > 0) {
            --silent_next;
            return;
        }
        last_reply_ = r;
        last_valid_ = true;
        queue_block(r, false);
    }

    bool read(RxByte& out, uint32_t timeout_us) override {
        if (head_ >= tail_) {
            clock_.advance_us(timeout_us);
            return false;
        }
        const RxByte& b = rx_[head_];
        if (b.t_us > clock_.now_us() + timeout_us) {
            clock_.advance_us(timeout_us);
            return false;
        }
        if (b.t_us > clock_.now_us()) clock_.us = b.t_us;
        out = b;
        ++head_;
        return true;
    }

    void flush_rx() override { head_ = tail_ = 0; }

private:
    void queue_block(const Response& r, bool is_retransmit) {
        uint8_t bytes[kMaxBlock + 1];
        bool modes[kMaxBlock + 1];
        int n = 0;
        if (r.kind == Reply::Ack || r.kind == Reply::Nak) {
            bytes[0] = r.kind == Reply::Ack ? kAck : kNak;
            modes[0] = true;
            n = 1;
        } else {
            for (uint8_t i = 0; i < r.len; ++i) {
                bytes[n] = r.data[i];
                modes[n++] = false;
            }
            bytes[n] = checksum(r.data, r.len);
            modes[n++] = !drop_checksum_mode_bit;
            if (corrupt_next && (!is_retransmit || corrupt_retransmit)) {
                bytes[n - 1] ^= 0x01;
                if (is_retransmit || !corrupt_retransmit) corrupt_next = false;
            }
        }
        uint64_t t = clock_.now_us() + gap_us;
        for (int i = 0; i < n && tail_ < kRx; ++i, t += gap_us) {
            rx_[tail_].data = bytes[i];
            rx_[tail_].mode = modes[i];
            rx_[tail_].t_us = t;
            ++tail_;
        }
    }

    static constexpr int kRx = 128;
    FakeClock& clock_;
    FakePeripheral* peers_[kMaxPeripherals] = {};
    uint8_t addrs_[kMaxPeripherals] = {};
    FakePeripheral* last_peer_ = nullptr;
    Response last_reply_{};
    bool last_valid_ = false;
    RxByte rx_[kRx];
    int head_ = 0, tail_ = 0;
};

}  // namespace mdbcore
