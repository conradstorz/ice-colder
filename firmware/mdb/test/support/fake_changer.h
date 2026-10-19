// Byte-level fake MDB coin changer (address 0x08). Fixed arrays only.
#pragma once

#include <string.h>

#include "fakes.h"

namespace mdbcore {

struct FakeChanger : FakePeripheral {
    // Configuration.
    uint8_t level = 3;
    uint8_t scaling = 5;
    uint8_t decimals = 2;
    uint8_t credit[16] = {1, 2, 5, 20, 0xFF};  // 5c 10c 25c $1 token (units of `scaling`)
    uint16_t routing = 0x0007;                 // types 0-2 to tubes; $1 to the cashbox
    uint8_t tubes[16] = {10, 10, 10};
    uint16_t full_mask = 0;
    bool alt_payout = true;  // advertised in IDENTIFICATION
    int busy_polls = 0;      // PAYOUT STATUS ACKs (still busy) before the 16-byte reply
    bool fail_payout = false;
    bool high_bit_model = false;  // IDENTIFICATION model starts with two bytes >= 0x80
    int dispense_busy_polls = 0;  // level 2: POLLs reporting 0x02 (payout busy) before a DISPENSE settles in the tubes
    bool fail_coin_type = false;  // NAK every COIN TYPE
    uint8_t value_poll_cap_units = 255;  // PAYOUT VALUE POLL reports at most this many units
    bool silent = false;     // answer nothing at all

    // Scripted POLL blocks.
    static constexpr int kBlocks = 16;
    uint8_t blk[kBlocks][16] = {};
    uint8_t blk_len[kBlocks] = {};
    int n_blk = 0, head = 0;

    // Recording.
    bool features_enabled = false;
    uint16_t coin_masks[16] = {};
    int n_masks = 0;
    uint8_t manual_mask_bytes = 0;  // sum of the manual-dispense mask bytes of the last COIN TYPE
    uint8_t dispenses[16] = {};     // count<<4 | type
    int n_disp = 0;
    uint8_t payouts[8] = {};
    int n_payouts = 0;
    int calls_total = 0;
    int calls[8] = {};

    bool payout_pending = false;
    int busy_left = 0;
    uint8_t settling[16] = {};  // coins a DISPENSE took that are not yet reflected in `tubes`
    uint8_t paid_types[16] = {};
    int status_polls = 0;

    void clear_script() { n_blk = head = 0; }
    void add(uint8_t b, bool same_block) {
        if (!same_block || n_blk == 0) {
            if (n_blk >= kBlocks) return;
            blk_len[n_blk] = 0;
            ++n_blk;
        }
        blk[n_blk - 1][blk_len[n_blk - 1]++] = b;
    }
    void deposit(uint8_t type, uint8_t route, bool same_block = false) {
        if (route == 1 && tubes[type] < 255) ++tubes[type];
        add(static_cast<uint8_t>(0x40 | (route << 4) | type), same_block);
        add(route == 1 ? tubes[type] : 0, true);
    }
    void slug(bool same_block = false) { add(0x20, same_block); }
    void lever(bool same_block = false) { add(0x01, same_block); }
    void status(uint8_t b, bool same_block = false) { add(b, same_block); }
    void just_reset() { add(0x0B, false); }

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

    // Honest greedy payout from the tubes, in scaled units.
    void pay_units(uint8_t units) {
        memset(paid_types, 0, sizeof(paid_types));
        uint8_t rem = units;
        for (;;) {
            int best = -1;
            for (uint8_t t = 0; t < 16; ++t) {
                if (!((routing >> t) & 1) || credit[t] == 0 || credit[t] == 0xFF || tubes[t] == 0) continue;
                if (credit[t] > rem) continue;
                if (best < 0 || credit[t] > credit[best]) best = t;
            }
            if (best < 0) break;
            --tubes[best];
            ++paid_types[best];
            rem = static_cast<uint8_t>(rem - credit[best]);
        }
    }

    Response respond(uint8_t cmd, const uint8_t* d, uint8_t n) override {
        if (silent) {
            Response t{};
            t.kind = Reply::Timeout;
            return t;
        }
        ++calls_total;
        ++calls[cmd & 7];
        uint8_t out[40];
        switch (cmd) {
            case 0:
                clear_script();
                just_reset();
                payout_pending = false;
                return ack();
            case 1: {
                out[0] = level;
                out[1] = 0x18;
                out[2] = 0x40;
                out[3] = scaling;
                out[4] = decimals;
                out[5] = static_cast<uint8_t>(routing >> 8);
                out[6] = static_cast<uint8_t>(routing & 0xFF);
                for (int t = 0; t < 16; ++t) out[7 + t] = credit[t];
                return data(out, 23);
            }
            case 2: {
                out[0] = static_cast<uint8_t>(full_mask >> 8);
                out[1] = static_cast<uint8_t>(full_mask & 0xFF);
                for (int t = 0; t < 16; ++t) out[2 + t] = tubes[t];
                return data(out, 18);
            }
            case 3: {
                if (busy_left > 0) {
                    if (--busy_left == 0)
                        for (int t = 0; t < 16; ++t) {
                            tubes[t] = static_cast<uint8_t>(tubes[t] - settling[t]);
                            settling[t] = 0;
                        }
                    const uint8_t busy = 0x02;
                    return data(&busy, 1);
                }
                if (head < n_blk) {
                    const int i = head++;
                    return data(blk[i], blk_len[i]);
                }
                return ack();
            }
            case 4: {
                if (n < 4 || fail_coin_type) return nak();
                if (n_masks < 16) coin_masks[n_masks++] = static_cast<uint16_t>((d[0] << 8) | d[1]);
                manual_mask_bytes = static_cast<uint8_t>(d[2] + d[3]);
                return ack();
            }
            case 5: {
                if (n < 1) return nak();
                if (n_disp < 16) dispenses[n_disp++] = d[0];
                const uint8_t t = d[0] & 0x0F;
                uint8_t cnt = d[0] >> 4;
                while (cnt > 0 && tubes[t] > 0) {
                    --tubes[t];
                    if (dispense_busy_polls > 0) {
                        ++tubes[t];  // keep showing the old count until the busy window ends
                        ++settling[t];
                    }
                    --cnt;
                }
                if (dispense_busy_polls > 0) busy_left = dispense_busy_polls;
                return ack();
            }
            case 7:
                break;
            default:
                return nak();
        }
        if (n < 1) return nak();
        switch (d[0]) {
            case 0x00: {
                memset(out, ' ', 33);
                memcpy(out, "KLR", 3);
                memcpy(out + 3, "SER000000042", 12);
                memcpy(out + 15, "FAKECHG-3   ", 12);
                if (high_bit_model) {
                    out[15] = 0xC3;
                    out[16] = 0xA9;
                }
                out[27] = 1;
                out[28] = 0;
                out[29] = out[30] = out[31] = 0;
                out[32] = alt_payout ? 0x01 : 0x00;
                return data(out, 33);
            }
            case 0x01:
                features_enabled = n >= 5 && (d[4] & 1);
                return ack();
            case 0x02: {
                if (n < 2 || fail_payout) return nak();
                if (n_payouts < 8) payouts[n_payouts++] = d[1];
                pay_units(d[1]);
                payout_pending = true;
                status_polls = 0;
                return ack();
            }
            case 0x03: {
                if (!payout_pending) return ack();
                if (status_polls++ < busy_polls) return ack();
                payout_pending = false;
                return data(paid_types, 16);
            }
            case 0x04: {
                if (!payout_pending) return ack();
                uint8_t units = 0;
                for (int t = 0; t < 16; ++t) units = static_cast<uint8_t>(units + paid_types[t] * credit[t]);
                out[0] = units < value_poll_cap_units ? units : value_poll_cap_units;
                return data(out, 1);
            }
            default:
                return nak();
        }
    }
};

}  // namespace mdbcore
