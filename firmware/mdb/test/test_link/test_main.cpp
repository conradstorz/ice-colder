#include <string.h>
#include <unity.h>

#include "../support/fakes.h"
#include "link.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

namespace {

struct Scripted : FakePeripheral {
    Response next{};
    uint8_t last_cmd = 0xFF;
    uint8_t last_n = 0;
    int calls = 0;
    uint8_t master_bytes[8] = {};
    int n_master = 0;
    Response respond(uint8_t cmd, const uint8_t*, uint8_t n) override {
        ++calls;
        last_cmd = cmd;
        last_n = n;
        return next;
    }
    void on_master_byte(uint8_t b) override {
        if (n_master < 8) master_bytes[n_master++] = b;
    }
    void reply_ack() {
        next = Response{};
        next.kind = Reply::Ack;
    }
    void reply_nak() {
        next = Response{};
        next.kind = Reply::Nak;
    }
    void reply_data(const uint8_t* d, uint8_t n) {
        next = Response{};
        next.kind = Reply::Data;
        memcpy(next.data, d, n);
        next.len = n;
    }
};

struct Rig {
    FakeClock clock;
    FakeBus bus{clock};
    Scripted dev;
    Link link{bus, clock};
    Rig() { bus.attach(0x08, &dev); }
};

}  // namespace

static void test_reset_framing(void) {
    Rig r;
    r.dev.reply_ack();
    Response resp = r.link.transact(0x08, 0x00, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Ack, resp.kind);
    TEST_ASSERT_EQUAL_INT(2, r.bus.tx_len);
    TEST_ASSERT_EQUAL_HEX8(0x08, r.bus.tx_bytes[0]);
    TEST_ASSERT_TRUE(r.bus.tx_modes[0]);
    TEST_ASSERT_EQUAL_HEX8(0x08, r.bus.tx_bytes[1]);
    TEST_ASSERT_FALSE(r.bus.tx_modes[1]);
}

static void test_data_command_framing(void) {
    Rig r;
    r.dev.reply_ack();
    const uint8_t d[2] = {0x01, 0x02};
    r.link.transact(0x08, 0x04, d, 2);
    TEST_ASSERT_EQUAL_INT(4, r.bus.tx_len);
    TEST_ASSERT_EQUAL_HEX8(0x0C, r.bus.tx_bytes[0]);
    TEST_ASSERT_FALSE(r.bus.tx_modes[1]);
    TEST_ASSERT_EQUAL_HEX8(0x0F, r.bus.tx_bytes[3]);
    TEST_ASSERT_EQUAL_UINT8(4, r.dev.last_cmd);
    TEST_ASSERT_EQUAL_UINT8(2, r.dev.last_n);
}

static void test_parity_truth_table(void) {
    TEST_ASSERT_TRUE(parity_even_for(0x01, true));  // odd popcount: even parity yields bit 1
    TEST_ASSERT_FALSE(parity_even_for(0x01, false));
    TEST_ASSERT_FALSE(parity_even_for(0x03, true));  // even popcount: even parity yields bit 0
    TEST_ASSERT_TRUE(parity_even_for(0x03, false));
    TEST_ASSERT_TRUE(parity_even_for(0x00, false));
    TEST_ASSERT_FALSE(parity_even_for(0x00, true));
    TEST_ASSERT_FALSE(parity_even_for(0xFF, true));
    TEST_ASSERT_TRUE(parity_even_for(0xFF, false));
}

static void test_ack_and_nak(void) {
    Rig r;
    r.dev.reply_ack();
    TEST_ASSERT_EQUAL(Reply::Ack, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    r.dev.reply_nak();
    TEST_ASSERT_EQUAL(Reply::Nak, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x08));
}

static void test_data_block_mode_bit_terminator(void) {
    Rig r;
    const uint8_t d[3] = {0x01, 0x02, 0x03};
    r.dev.reply_data(d, 3);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Data, resp.kind);
    TEST_ASSERT_EQUAL_UINT8(3, resp.len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(d, resp.data, 3);
    TEST_ASSERT_EQUAL_UINT32(0, r.link.mode_disagreements());
    r.link.acknowledge();
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_master);
    TEST_ASSERT_EQUAL_HEX8(kAck, r.dev.master_bytes[0]);
}

static void test_data_block_idle_gap_terminator(void) {
    Rig r;
    r.bus.drop_checksum_mode_bit = true;
    const uint8_t d[3] = {0x01, 0x02, 0x03};
    r.dev.reply_data(d, 3);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Data, resp.kind);
    TEST_ASSERT_EQUAL_UINT8(3, resp.len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(d, resp.data, 3);
    TEST_ASSERT_EQUAL_UINT32(1, r.link.mode_disagreements());
}

static void test_corrupt_then_ret_recovers(void) {
    Rig r;
    r.bus.corrupt_next = true;
    const uint8_t d[2] = {0x10, 0x20};
    r.dev.reply_data(d, 2);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Data, resp.kind);
    TEST_ASSERT_EQUAL_UINT8(2, resp.len);
    TEST_ASSERT_EQUAL_INT(1, r.dev.n_master);
    TEST_ASSERT_EQUAL_HEX8(kRet, r.dev.master_bytes[0]);
    TEST_ASSERT_EQUAL_UINT32(1, r.link.corrupt_blocks());
}

static void test_second_corrupt_is_nak(void) {
    Rig r;
    r.bus.corrupt_next = true;
    r.bus.corrupt_retransmit = true;
    const uint8_t d[2] = {0x10, 0x20};
    r.dev.reply_data(d, 2);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Corrupt, resp.kind);
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_master);
    TEST_ASSERT_EQUAL_HEX8(kRet, r.dev.master_bytes[0]);
    TEST_ASSERT_EQUAL_HEX8(kNak, r.dev.master_bytes[1]);
    TEST_ASSERT_EQUAL_UINT32(2, r.link.corrupt_blocks());
    TEST_ASSERT_EQUAL_INT(1, r.link.misses(0x08));
}

static void test_one_silent_reply_retries(void) {
    Rig r;
    r.dev.reply_ack();
    r.bus.silent_next = 1;
    TEST_ASSERT_EQUAL(Reply::Ack, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(2, r.dev.calls);
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x08));
}

static void test_two_silent_replies_timeout(void) {
    Rig r;
    r.dev.reply_ack();
    r.bus.silent_next = 2;
    TEST_ASSERT_EQUAL(Reply::Timeout, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(1, r.link.misses(0x08));
}

static void test_ten_timeouts_reach_threshold_and_valid_reply_clears(void) {
    Rig r;
    r.dev.reply_ack();
    for (int i = 0; i < kMissThreshold; ++i) {
        r.bus.silent_next = 2;
        r.link.transact(0x08, 0x0B, nullptr, 0);
    }
    TEST_ASSERT_EQUAL_INT(kMissThreshold, r.link.misses(0x08));
    TEST_ASSERT_EQUAL(Reply::Ack, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x08));
    r.bus.silent_next = 2;
    r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL_INT(1, r.link.misses(0x08));
    r.link.clear_misses(0x08);
    TEST_ASSERT_EQUAL_INT(0, r.link.misses(0x08));
}

static void test_overlong_block_is_corrupt(void) {
    Rig r;
    uint8_t d[kMaxBlock];
    for (uint8_t i = 0; i < kMaxBlock; ++i) d[i] = 1;
    r.dev.reply_data(d, kMaxBlock);  // 36 data + checksum = 37 bytes
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Corrupt, resp.kind);
}

static void test_silence_after_ret_does_not_resend(void) {
    Rig r;
    r.bus.corrupt_next = true;
    r.bus.silent_retransmit = true;
    const uint8_t d[2] = {0x10, 0x20};
    r.dev.reply_data(d, 2);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Corrupt, resp.kind);
    TEST_ASSERT_EQUAL_INT(1, r.dev.calls);  // the command was never repeated
    TEST_ASSERT_EQUAL_INT(4, r.bus.tx_len);  // command (2 bytes) + RET + NAK
    TEST_ASSERT_EQUAL_INT(2, r.dev.n_master);
    TEST_ASSERT_EQUAL_HEX8(kRet, r.dev.master_bytes[0]);
    TEST_ASSERT_EQUAL_HEX8(kNak, r.dev.master_bytes[1]);
    TEST_ASSERT_EQUAL_INT(1, r.link.misses(0x08));
}

static void test_first_byte_after_5ms_is_timeout(void) {
    Rig r;
    r.dev.reply_ack();
    r.bus.first_byte_delay_us = kResponseTimeoutUs + 1000;
    TEST_ASSERT_EQUAL(Reply::Timeout, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(2, r.dev.calls);
    TEST_ASSERT_EQUAL_INT(1, r.link.misses(0x08));
}

static void test_first_byte_at_5ms_is_accepted(void) {
    Rig r;
    r.dev.reply_ack();
    r.bus.first_byte_delay_us = kResponseTimeoutUs;
    TEST_ASSERT_EQUAL(Reply::Ack, r.link.transact(0x08, 0x0B, nullptr, 0).kind);
    TEST_ASSERT_EQUAL_INT(1, r.dev.calls);
}

static void test_inter_byte_gap_over_1ms_ends_block_and_triggers_ret(void) {
    Rig r;
    r.bus.gap_us = 1200;
    const uint8_t d[3] = {0x01, 0x02, 0x03};
    r.dev.reply_data(d, 3);
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Corrupt, resp.kind);
    TEST_ASSERT_EQUAL_INT(4, r.bus.tx_len);
    TEST_ASSERT_EQUAL_HEX8(kRet, r.bus.tx_bytes[2]);
    TEST_ASSERT_EQUAL_HEX8(kNak, r.bus.tx_bytes[3]);
    TEST_ASSERT_EQUAL_UINT32(2, r.link.mode_disagreements());
}

static void test_max_length_reply_accepted(void) {
    Rig r;
    uint8_t d[35];
    for (uint8_t i = 0; i < 35; ++i) d[i] = i;
    r.dev.reply_data(d, 35);  // 35 data + checksum = 36 bytes
    Response resp = r.link.transact(0x08, 0x0B, nullptr, 0);
    TEST_ASSERT_EQUAL(Reply::Data, resp.kind);
    TEST_ASSERT_EQUAL_UINT8(35, resp.len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(d, resp.data, 35);
}

static void test_command_data_over_34_is_refused(void) {
    Rig r;
    r.dev.reply_ack();
    uint8_t d[35] = {};
    Response resp = r.link.transact(0x08, 0x0B, d, 35);
    TEST_ASSERT_EQUAL(Reply::Corrupt, resp.kind);
    TEST_ASSERT_EQUAL_INT(0, r.bus.tx_len);
    TEST_ASSERT_EQUAL_INT(0, r.dev.calls);
    TEST_ASSERT_EQUAL(Reply::Ack, r.link.transact(0x08, 0x0B, d, 34).kind);  // 34 is the limit
    TEST_ASSERT_EQUAL_UINT8(34, r.dev.last_n);
}

static void test_command_is_masked_to_three_bits(void) {
    Rig r;
    r.link.transact(0x10, 0x0F, nullptr, 0);  // no peer at 0x10; only the transcript matters
    TEST_ASSERT_EQUAL_HEX8(0x17, r.bus.tx_bytes[0]);
}

static void test_rx_deadline_adds_one_character_time_and_margin(void) {
    TEST_ASSERT_EQUAL_UINT32(1150 + 250, rx_deadline_us(0));
    TEST_ASSERT_EQUAL_UINT32(kInterByteGapUs + kCharTimeUs + kRxMarginUs, rx_deadline_us(kInterByteGapUs));
    TEST_ASSERT_TRUE(rx_deadline_us(kInterByteGapUs) > 2000);  // the 9600 baud stop bit ends 1.146 ms after the start
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_rx_deadline_adds_one_character_time_and_margin);
    RUN_TEST(test_reset_framing);
    RUN_TEST(test_data_command_framing);
    RUN_TEST(test_parity_truth_table);
    RUN_TEST(test_ack_and_nak);
    RUN_TEST(test_data_block_mode_bit_terminator);
    RUN_TEST(test_data_block_idle_gap_terminator);
    RUN_TEST(test_corrupt_then_ret_recovers);
    RUN_TEST(test_second_corrupt_is_nak);
    RUN_TEST(test_one_silent_reply_retries);
    RUN_TEST(test_two_silent_replies_timeout);
    RUN_TEST(test_ten_timeouts_reach_threshold_and_valid_reply_clears);
    RUN_TEST(test_overlong_block_is_corrupt);
    RUN_TEST(test_silence_after_ret_does_not_resend);
    RUN_TEST(test_first_byte_after_5ms_is_timeout);
    RUN_TEST(test_first_byte_at_5ms_is_accepted);
    RUN_TEST(test_inter_byte_gap_over_1ms_ends_block_and_triggers_ret);
    RUN_TEST(test_max_length_reply_accepted);
    RUN_TEST(test_command_data_over_34_is_refused);
    RUN_TEST(test_command_is_masked_to_three_bits);
    return UNITY_END();
}
