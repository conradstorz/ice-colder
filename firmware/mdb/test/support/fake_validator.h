// Byte-level fake MDB bill validator (address 0x30). Fixed arrays only.
#pragma once

#include <string.h>

#include "fakes.h"

namespace mdbcore {

struct FakeValidator : FakePeripheral {
    // Configuration.
    uint8_t level = 2;
    uint16_t scaling = 100;
    uint8_t decimals = 2;
    uint16_t stacker_capacity = 500;
    uint8_t credit[16] = {1, 5, 10, 20};  // $1 $5 $10 $20 (units of `scaling`)
    bool escrow_capable = true;
    bool silent = false;  // answer nothing at all
    bool fail_escrow = false;  // NAK every ESCROW command

    // Live state.
    uint16_t stacker_count = 0;
    bool cashbox_is_out = false;
    bool in_escrow = false;
    uint8_t escrow_type = 0;
    uint16_t bill_mask = 0, escrow_mask = 0;  // last BILL TYPE

    // Scripted POLL blocks.
    static constexpr int kBlocks = 16;
    uint8_t blk[kBlocks][16] = {};
    uint8_t blk_len[kBlocks] = {};
    int n_blk = 0, head = 0;

    // Recording.
    uint16_t bill_masks[16] = {}, escrow_masks[16] = {};
    int n_masks = 0;
    uint8_t escrow_cmds[16] = {};
    int n_escrow_cmds = 0;
    int calls[8] = {};

    void clear_script() { n_blk = head = 0; }
    void add(uint8_t b, bool same_block = false) {
        if (!same_block || n_blk == 0) {
            if (n_blk >= kBlocks) return;
            blk_len[n_blk] = 0;
            ++n_blk;
        }
        blk[n_blk - 1][blk_len[n_blk - 1]++] = b;
    }
    void status(uint8_t b, bool same_block = false) { add(b, same_block); }
    void just_reset() { add(0x06, false); }
    void cashbox_out() { cashbox_is_out = true; }
    void cashbox_in() { cashbox_is_out = false; }

    bool stacker_full() const { return stacker_count >= stacker_capacity; }

    // A customer feeds a bill of `type`: escrow when capable and enabled for escrow, else straight
    // to the stacker; a disabled type is rejected.
    void insert(uint8_t type, bool same_block = false) {
        if (!((bill_mask >> type) & 1)) {
            add(static_cast<uint8_t>(0xC0 | type), same_block);  // 100: disabled bill rejected
        } else if (escrow_capable && ((escrow_mask >> type) & 1)) {
            in_escrow = true;
            escrow_type = type;
            add(static_cast<uint8_t>(0x90 | type), same_block);  // 001: escrow position
        } else {
            ++stacker_count;
            add(static_cast<uint8_t>(0x80 | type), same_block);  // 000: stacked
        }
    }

    static Response ack() {
        Response r{};
        r.kind = Reply::Ack;
        return r;
    }
    static Response nak() {
        Response r{};
        r.kind = Reply::Nak;
        return r;
    }
    static Response data(const uint8_t* d, uint8_t n) {
        Response r{};
        r.kind = Reply::Data;
        memcpy(r.data, d, n);
        r.len = n;
        return r;
    }

    Response respond(uint8_t cmd, const uint8_t* d, uint8_t n) override {
        if (silent) {
            Response t{};
            t.kind = Reply::Timeout;
            return t;
        }
        ++calls[cmd & 7];
        uint8_t out[40];
        switch (cmd) {
            case 0:
                clear_script();
                in_escrow = false;
                just_reset();
                return ack();
            case 1: {
                out[0] = level;
                out[1] = 0x18;
                out[2] = 0x40;
                out[3] = static_cast<uint8_t>(scaling >> 8);
                out[4] = static_cast<uint8_t>(scaling & 0xFF);
                out[5] = decimals;
                out[6] = static_cast<uint8_t>(stacker_capacity >> 8);
                out[7] = static_cast<uint8_t>(stacker_capacity & 0xFF);
                out[8] = 0;
                out[9] = 0;
                out[10] = escrow_capable ? 0xFF : 0x00;
                for (int t = 0; t < 16; ++t) out[11 + t] = credit[t];
                return data(out, 27);
            }
            case 3: {
                if (head < n_blk) {
                    const int i = head++;
                    return data(blk[i], blk_len[i]);
                }
                if (cashbox_is_out) {
                    out[0] = 0x08;
                    return data(out, 1);
                }
                return ack();
            }
            case 4: {
                if (n < 4) return nak();
                bill_mask = static_cast<uint16_t>((d[0] << 8) | d[1]);
                escrow_mask = static_cast<uint16_t>((d[2] << 8) | d[3]);
                if (n_masks < 16) {
                    bill_masks[n_masks] = bill_mask;
                    escrow_masks[n_masks++] = escrow_mask;
                }
                return ack();
            }
            case 5: {
                if (n < 1 || fail_escrow) return nak();
                if (n_escrow_cmds < 16) escrow_cmds[n_escrow_cmds++] = d[0];
                if (!in_escrow) {
                    status(0x0A);  // invalid escrow request
                    return ack();
                }
                in_escrow = false;
                if (d[0] == 0x01) {
                    ++stacker_count;
                    add(static_cast<uint8_t>(0x80 | escrow_type));  // stacked
                } else {
                    add(static_cast<uint8_t>(0xA0 | escrow_type));  // 010: returned
                }
                return ack();
            }
            case 6: {
                const uint16_t v = static_cast<uint16_t>((stacker_full() ? 0x8000 : 0) | (stacker_count & 0x7FFF));
                out[0] = static_cast<uint8_t>(v >> 8);
                out[1] = static_cast<uint8_t>(v & 0xFF);
                return data(out, 2);
            }
            case 7:
                break;
            default:
                return nak();
        }
        if (n < 1 || d[0] != 0x00) return nak();
        memset(out, ' ', 29);
        memcpy(out, "KLR", 3);
        memcpy(out + 3, "SERV00000042", 12);
        memcpy(out + 15, "FAKEVAL-2   ", 12);
        out[27] = 1;
        out[28] = 0;
        return data(out, 29);
    }
};

}  // namespace mdbcore
