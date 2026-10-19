// Byte-level fake MDB cashless reader (address 0x10). Fixed arrays only.
#pragma once

#include <string.h>

#include "fakes.h"

namespace mdbcore {

struct FakeReader : FakePeripheral {
    // Configuration.
    uint8_t level = 1;
    uint8_t scale = 5;  // price/funds unit in cents
    uint8_t decimals = 2;
    uint8_t max_response_s = 5;
    bool offer_always_idle = false;  // level 3 only, and only when the VMC announced level 3
    bool silent = false;             // answer nothing at all
    bool fail_next_action = false;   // NAK the next VEND or READER command (then clears itself)

    // Live state.
    bool reader_enabled = false;
    bool in_session = false;
    bool always_idle_on = false;
    int next_vend = 0;  // 0 hold (no answer), 1 approve, 2 deny
    uint16_t approve_units = 0;

    // Scripted POLL blocks.
    static constexpr int kBlocks = 16;
    uint8_t blk[kBlocks][16] = {};
    uint8_t blk_len[kBlocks] = {};
    int n_blk = 0, head = 0;

    // Recording.
    uint8_t vmc_levels[8] = {};
    int n_cfg = 0;
    uint16_t max_price = 0, min_price = 0;
    int n_prices = 0;
    uint8_t reader_cmds[16] = {};
    int n_reader_cmds = 0;
    struct Vend {
        uint8_t sub;
        uint16_t price;
        uint16_t item;
        int len;
    };
    Vend vends[16] = {};
    int n_vends = 0;
    uint8_t features[4] = {};
    int n_features = 0;
    uint8_t id_request[32] = {};
    int id_request_len = 0;
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

    // Scripted reader behaviour.
    void tap(uint16_t funds_units) {  // 0xFFFF = funds undisclosed
        in_session = true;
        add(0x03);
        add(static_cast<uint8_t>(funds_units >> 8), true);
        add(static_cast<uint8_t>(funds_units & 0xFF), true);
    }
    void cancel_session() { add(0x04); }
    void approve(uint32_t cents) {
        next_vend = 1;
        approve_units = static_cast<uint16_t>(cents / scale);
    }
    void deny() { next_vend = 2; }
    void malfunction(uint8_t code) {
        add(0x0A);
        add(code, true);
    }
    void out_of_sequence() { add(0x0B); }
    void just_reset() { add(0x00); }
    void end_session_by_reader() {
        in_session = false;
        add(0x07);
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
            case 0:  // RESET
                clear_script();
                in_session = false;
                reader_enabled = false;
                always_idle_on = false;
                just_reset();
                return ack();
            case 1:  // SETUP
                if (n < 1) return nak();
                if (d[0] == 0x00) {
                    if (n_cfg < 8) vmc_levels[n_cfg++] = n > 1 ? d[1] : 0;
                    out[0] = 0x01;
                    out[1] = level;
                    out[2] = 0x18;  // USD
                    out[3] = 0x40;
                    out[4] = scale;
                    out[5] = decimals;
                    out[6] = max_response_s;
                    out[7] = 0x00;
                    return data(out, 8);
                }
                if (n >= 5) {
                    max_price = static_cast<uint16_t>((d[1] << 8) | d[2]);
                    min_price = static_cast<uint16_t>((d[3] << 8) | d[4]);
                    ++n_prices;
                }
                return ack();
            case 2:  // POLL
                if (head < n_blk) {
                    const int i = head++;
                    return data(blk[i], blk_len[i]);
                }
                return ack();
            case 3:  // VEND
                if (n < 1) return nak();
                if (fail_next_action) {
                    fail_next_action = false;
                    return nak();
                }
                if (n_vends < 16) {
                    Vend& v = vends[n_vends++];
                    v.sub = d[0];
                    v.price = (d[0] == 0x00 || d[0] == 0x05) && n >= 5 ? static_cast<uint16_t>((d[1] << 8) | d[2]) : 0;
                    v.item = d[0] == 0x02 && n >= 3 ? static_cast<uint16_t>((d[1] << 8) | d[2])
                             : ((d[0] == 0x00 || d[0] == 0x05) && n >= 5) ? static_cast<uint16_t>((d[3] << 8) | d[4])
                                                                          : 0;
                    v.len = n;
                }
                switch (d[0]) {
                    case 0x00:
                        if (!in_session && !always_idle_on) {
                            out_of_sequence();
                        } else if (next_vend == 1) {
                            add(0x05);
                            add(static_cast<uint8_t>(approve_units >> 8), true);
                            add(static_cast<uint8_t>(approve_units & 0xFF), true);
                            next_vend = 0;
                        } else if (next_vend == 2) {
                            add(0x06);
                            next_vend = 0;
                        }
                        return ack();
                    case 0x01:  // VEND CANCEL
                        add(0x06);
                        return ack();
                    case 0x04:  // SESSION COMPLETE
                        in_session = false;
                        add(0x07);
                        return ack();
                    default:
                        return ack();
                }
            case 4:  // READER
                if (n < 1) return nak();
                if (fail_next_action) {
                    fail_next_action = false;
                    return nak();
                }
                if (n_reader_cmds < 16) reader_cmds[n_reader_cmds++] = d[0];
                if (d[0] == 0x00) reader_enabled = false;
                if (d[0] == 0x01) reader_enabled = true;
                if (d[0] == 0x02) add(0x08);
                return ack();
            case 7:  // EXPANSION
                break;
            default:
                return nak();
        }
        if (n < 1) return nak();
        if (d[0] == 0x00) {
            id_request_len = n < 32 ? n : 32;
            memcpy(id_request, d, id_request_len);
            memset(out, ' ', 34);
            out[0] = 0x09;
            memcpy(out + 1, "FKR", 3);
            memcpy(out + 4, "RDR000000007", 12);
            memcpy(out + 16, "FAKECASHLESS", 12);
            out[28] = 0;
            out[29] = 1;
            if (level < 3) return data(out, 30);
            memset(out + 30, 0, 4);
            const bool vmc3 = n_cfg > 0 && vmc_levels[n_cfg - 1] >= 3;
            if (offer_always_idle && vmc3) out[33] = 0x02;
            return data(out, 34);
        }
        if (d[0] == 0x04 && n >= 5) {
            ++n_features;
            memcpy(features, d + 1, 4);
            if (offer_always_idle && (d[4] & 0x02)) always_idle_on = true;
            return ack();
        }
        return nak();
    }
};

}  // namespace mdbcore
