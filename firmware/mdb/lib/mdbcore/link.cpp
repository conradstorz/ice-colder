#include "link.h"

namespace mdbcore {

bool parity_even_for(uint8_t data, bool mode) {
    uint8_t ones = 0;
    for (uint8_t v = data; v; v >>= 1) ones += v & 1;
    return ((ones & 1) != 0) == mode;
}

uint8_t checksum(const uint8_t* bytes, uint8_t n) {
    uint8_t sum = 0;
    for (uint8_t i = 0; i < n; ++i) sum = static_cast<uint8_t>(sum + bytes[i]);
    return sum;
}

void Link::send_master_byte(uint8_t b) {
    const bool mode = false;
    bus_.write(&b, &mode, 1);
}

void Link::acknowledge() { send_master_byte(kAck); }

// Reads one peripheral reply. A block ends on a byte carrying mode bit 1, or after a
// kInterByteGapUs silence (used when the mode bit was not decoded).
Response Link::read_response() {
    Response r{};
    r.kind = Reply::Timeout;
    uint8_t buf[kMaxBlock];
    uint16_t total = 0;
    bool ended_by_mode = false;
    RxByte rx{};
    if (!bus_.read(rx, kResponseTimeoutUs)) return r;
    for (;;) {
        if (total < kMaxBlock) buf[total] = rx.data;
        ++total;
        if (rx.mode) {
            ended_by_mode = true;
            break;
        }
        if (!bus_.read(rx, kInterByteGapUs)) break;
    }
    if (!ended_by_mode) ++mode_disagreements_;

    if (total > kMaxBlock) {
        r.kind = Reply::Corrupt;
        return r;
    }
    if (total == 1 && buf[0] == kAck) {
        r.kind = Reply::Ack;
        return r;
    }
    if (total == 1 && buf[0] == kNak) {
        r.kind = Reply::Nak;
        return r;
    }
    if (total < 2 || checksum(buf, static_cast<uint8_t>(total - 1)) != buf[total - 1]) {
        r.kind = Reply::Corrupt;
        return r;
    }
    r.kind = Reply::Data;
    r.len = static_cast<uint8_t>(total - 1);
    for (uint8_t i = 0; i < r.len; ++i) r.data[i] = buf[i];
    return r;
}

// Timeout here means the peripheral sent no byte at all, so the command was not executed.
// Once any reply block arrived the command ran: never report Timeout after that.
Response Link::send_once(const uint8_t* bytes, const bool* modes, uint8_t n) {
    bus_.flush_rx();
    bus_.write(bytes, modes, n);
    Response r = read_response();
    if (r.kind == Reply::Corrupt) {
        // One retransmit request, then give up with NAK.
        ++corrupt_blocks_;
        send_master_byte(kRet);
        r = read_response();
        if (r.kind == Reply::Timeout || r.kind == Reply::Corrupt) {
            // Silent or bad retransmit: the command already executed, so no resend.
            r.kind = Reply::Corrupt;
            ++corrupt_blocks_;
            send_master_byte(kNak);
        }
    }
    return r;
}

Response Link::transact(uint8_t address, uint8_t cmd, const uint8_t* data, uint8_t n) {
    Response bad{};
    bad.kind = Reply::Corrupt;
    if (n > kMaxBlock - 2) return bad;  // address|cmd + data + checksum must fit one block

    uint8_t bytes[kMaxBlock];
    bool modes[kMaxBlock];
    uint8_t len = 0;
    bytes[len] = static_cast<uint8_t>(address | (cmd & 0x07));  // cmd is 3 bits; never let it spill into the address
    modes[len++] = true;
    for (uint8_t i = 0; i < n; ++i) {
        bytes[len] = data[i];
        modes[len++] = false;
    }
    bytes[len] = checksum(bytes, len);
    modes[len++] = false;

    Response r = send_once(bytes, modes, len);
    if (r.kind == Reply::Timeout) r = send_once(bytes, modes, len);  // one retry of the whole command

    int& miss = misses_[(address >> 3) & 31];
    if (r.kind == Reply::Timeout) {
        ++miss;
    } else if (r.kind == Reply::Corrupt) {
        ++miss;
    } else {
        miss = 0;
    }
    return r;
}

}  // namespace mdbcore
