// MDB block layer: 9-bit framing, checksums, ACK/RET/NAK, retry and miss accounting.
#pragma once

#include <stdint.h>

#include "ports.h"

namespace mdbcore {

constexpr uint32_t kResponseTimeoutUs = 5000;
constexpr uint32_t kInterByteGapUs = 1000;
constexpr uint8_t kMaxBlock = 36;
constexpr uint8_t kAck = 0x00, kRet = 0xAA, kNak = 0xFF;
constexpr int kMissThreshold = 10;

// A byte reaches the port driver at the END of its stop bit: 11 bits at 9600 baud, 1.146 ms after
// its start bit. BusPort::read timeouts are line-idle times measured from that far end of the
// previous byte, so a port must add one character time plus margin before comparing against its
// own clock (a port that does not cuts multi-byte replies off and reports a legal late reply as a
// Timeout, which makes the Link resend the command).
constexpr uint32_t kCharTimeUs = 1150;
constexpr uint32_t kRxMarginUs = 250;
constexpr uint32_t rx_deadline_us(uint32_t timeout_us) { return timeout_us + kCharTimeUs + kRxMarginUs; }

// Which UART parity setting (true = even) makes the transmitted parity bit equal `mode`.
// Even parity makes the total number of ones (data + parity bit) even, so the bit equals popcount & 1.
bool parity_even_for(uint8_t data, bool mode);

uint8_t checksum(const uint8_t* bytes, uint8_t n);

enum class Reply : uint8_t { Ack, Nak, Data, Timeout, Corrupt };

struct Response {
    Reply kind;
    uint8_t data[kMaxBlock];
    uint8_t len;  // Data: payload length with the checksum stripped
};

class Link {
public:
    Link(BusPort& bus, Clock& clock) : bus_(bus), clock_(clock) {}

    // Sends address|cmd (mode bit 1), data (mode 0), checksum (mode 0), then reads the reply.
    // The caller sends acknowledge() after consuming a Data block.
    Response transact(uint8_t address, uint8_t cmd, const uint8_t* data, uint8_t n);
    void acknowledge();

    int misses(uint8_t address) const { return misses_[(address >> 3) & 31]; }
    void clear_misses(uint8_t address) { misses_[(address >> 3) & 31] = 0; }
    uint32_t mode_disagreements() const { return mode_disagreements_; }
    uint32_t corrupt_blocks() const { return corrupt_blocks_; }

private:
    Response send_once(const uint8_t* bytes, const bool* modes, uint8_t n);
    Response read_response();
    void send_master_byte(uint8_t b);

    BusPort& bus_;
    Clock& clock_;
    int misses_[32] = {};
    uint32_t mode_disagreements_ = 0;
    uint32_t corrupt_blocks_ = 0;
};

}  // namespace mdbcore
