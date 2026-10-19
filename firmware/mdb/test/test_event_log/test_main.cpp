#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "../support/fakes.h"
#include "event_log.h"
#include "wire.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

static FakeLogStore store;
static FakeIds ids;
static char out[600];

static PaymentEvent coin(uint32_t c) {
    PaymentEvent e = make_event(EventKind::CoinAccepted, Device::Coin);
    e.cents = c;
    e.route = CoinRoute::Tubes;
    return e;
}

static void reset() {
    store.n = 0;
    store.refuse = false;
    ids.counter = 0;
}

static bool entry_is(int i, const char* boot, uint32_t seq) {
    char buf[600], b[40];
    uint32_t s = 0;
    return store.get(i, buf, sizeof(buf)) && peek_boot_seq(buf, b, s) && strcmp(b, boot) == 0 && s == seq;
}

static void test_ids_and_envelope(void) {
    reset();
    EventLog log(store, ids);
    log.begin_boot();
    TEST_ASSERT_EQUAL_UINT(16, strlen(log.boot_id()));
    TEST_ASSERT_EQUAL_UINT32(0, log.seq_last());
    TEST_ASSERT_TRUE(log.append(coin(25), "2026-10-18T10:00:00Z", out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT32(1, log.seq_last());
    char expect[64];
    snprintf(expect, sizeof(expect), "\"event_id\":\"%s-1\"", log.boot_id());
    TEST_ASSERT_NOT_NULL(strstr(out, expect));
    TEST_ASSERT_NOT_NULL(strstr(out, "\"seq\":1"));
    TEST_ASSERT_NOT_NULL(strstr(out, "\"event\":{\"kind\":\"coin_accepted\",\"cents\":25,\"routed\":\"tubes\"}"));
    TEST_ASSERT_EQUAL_INT(1, log.pending());
    char back[600];
    TEST_ASSERT_TRUE(log.pending_at(0, back, sizeof(back)));
    TEST_ASSERT_EQUAL_STRING(out, back);
    char b[40];
    uint32_t s = 0;
    TEST_ASSERT_TRUE(peek_boot_seq(back, b, s));
    TEST_ASSERT_EQUAL_STRING(log.boot_id(), b);
    TEST_ASSERT_EQUAL_UINT32(1, s);
}

static void test_acked_same_boot(void) {
    reset();
    EventLog log(store, ids);
    log.begin_boot();
    for (int i = 0; i < 5; ++i) TEST_ASSERT_TRUE(log.append(coin(10), "t", out, sizeof(out)));
    log.acked(log.boot_id(), 3);
    TEST_ASSERT_EQUAL_INT(2, log.pending());
    TEST_ASSERT_TRUE(entry_is(0, log.boot_id(), 4));
    log.acked(log.boot_id(), 3);  // idempotent
    TEST_ASSERT_EQUAL_INT(2, log.pending());
    log.acked("0000000000000000", 99);  // unknown boot erases nothing
    TEST_ASSERT_EQUAL_INT(2, log.pending());
}

static void test_two_boots(void) {
    reset();
    EventLog log1(store, ids);
    log1.begin_boot();
    char boot1[17];
    for (int i = 0; i < 3; ++i) log1.append(coin(10), "t", out, sizeof(out));
    strcpy(boot1, log1.boot_id());
    EventLog log2(store, ids);  // reboot: same store, new id
    log2.begin_boot();
    TEST_ASSERT_TRUE(strcmp(boot1, log2.boot_id()) != 0);
    for (int i = 0; i < 2; ++i) log2.append(coin(10), "t", out, sizeof(out));
    TEST_ASSERT_EQUAL_UINT32(2, log2.seq_last());
    TEST_ASSERT_EQUAL_INT(5, log2.pending());
    // replay order: oldest boot first
    TEST_ASSERT_TRUE(entry_is(0, boot1, 1));
    TEST_ASSERT_TRUE(entry_is(2, boot1, 3));
    TEST_ASSERT_TRUE(entry_is(3, log2.boot_id(), 1));
    // an ack for the second boot cannot erase past the unacked first-boot prefix
    log2.acked(log2.boot_id(), 2);
    TEST_ASSERT_EQUAL_INT(5, log2.pending());
    // first-boot acks erase only first-boot entries
    log2.acked(boot1, 2);
    TEST_ASSERT_EQUAL_INT(3, log2.pending());
    TEST_ASSERT_TRUE(entry_is(0, boot1, 3));
    log2.acked(boot1, 3);
    TEST_ASSERT_EQUAL_INT(2, log2.pending());
    TEST_ASSERT_TRUE(entry_is(0, log2.boot_id(), 1));
    log2.acked(log2.boot_id(), 2);
    TEST_ASSERT_EQUAL_INT(0, log2.pending());
}

static void test_full_at_256(void) {
    reset();
    EventLog log(store, ids);
    log.begin_boot();
    for (int i = 0; i < 256; ++i) TEST_ASSERT_TRUE(log.append(coin(5), "t", out, sizeof(out)));
    TEST_ASSERT_TRUE(log.full());
    TEST_ASSERT_FALSE(log.append(coin(5), "t", out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT32(256, log.seq_last());
    log.acked(log.boot_id(), 1);
    TEST_ASSERT_FALSE(log.full());
    TEST_ASSERT_TRUE(log.append(coin(5), "t", out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT32(257, log.seq_last());
}

static void test_store_refusal_keeps_seq(void) {
    reset();
    EventLog log(store, ids);
    log.begin_boot();
    TEST_ASSERT_TRUE(log.append(coin(5), "t", out, sizeof(out)));
    store.refuse = true;
    TEST_ASSERT_FALSE(log.append(coin(5), "t", out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT32(1, log.seq_last());
    TEST_ASSERT_EQUAL_INT(1, log.pending());
    store.refuse = false;
    TEST_ASSERT_TRUE(log.append(coin(5), "t", out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT32(2, log.seq_last());
    TEST_ASSERT_NOT_NULL(strstr(out, "\"seq\":2"));
}

static void test_too_small_buffer_keeps_seq(void) {
    reset();
    EventLog log(store, ids);
    log.begin_boot();
    char tiny[20];
    TEST_ASSERT_FALSE(log.append(coin(5), "t", tiny, sizeof(tiny)));
    TEST_ASSERT_EQUAL_UINT32(0, log.seq_last());
    TEST_ASSERT_EQUAL_INT(0, log.pending());
}

static void test_event_json_all_kinds(void) {
    static char j[600];
    PaymentEvent e;
    auto render = [&](const PaymentEvent& ev) {
        TEST_ASSERT_GREATER_THAN_UINT(
            0, event_json(ev, "abcdefgh-1", "abcdefgh", 1, "2026-10-18T10:00:00Z", j, sizeof(j)));
    };
    e = make_event(EventKind::BillEscrowed, Device::Bill);
    e.cents = 500;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"event\":{\"kind\":\"bill_escrowed\",\"cents\":500}"));
    e = make_event(EventKind::BillStacked, Device::Bill);
    e.cents = 500;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"bill_stacked\",\"cents\":500}"));
    e = make_event(EventKind::BillReturned, Device::Bill);
    e.cents = 500;
    e.bill_reason = BillReturnReason::EdgeSilent;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"bill_returned\",\"cents\":500,\"reason\":\"edge_silent\"}"));
    e = make_event(EventKind::EscrowReturnPressed, Device::Bill);
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"event\":{\"kind\":\"escrow_return_pressed\"}"));
    e = make_event(EventKind::PayoutResult, Device::Coin);
    strcpy(e.request_id, "req-00000001");
    e.requested_cents = 300;
    e.paid_cents = 250;
    e.payout_status = PayoutStatus::Partial;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"payout_result\",\"request_id\":\"req-00000001\","
                                   "\"requested_cents\":300,\"paid_cents\":250,\"status\":\"partial\"}"));
    strcpy(e.detail, "tube empty");
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"detail\":\"tube empty\""));
    e = make_event(EventKind::SessionBegun, Device::Cashless);
    strcpy(e.session_id, "s1");
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"session_id\":\"s1\",\"funds_cents\":null"));
    e.funds_known = true;
    e.funds_cents = 1000;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"funds_cents\":1000"));
    e = make_event(EventKind::VendApproved, Device::Cashless);
    strcpy(e.session_id, "s1");
    strcpy(e.request_id, "req-00000002");
    e.cents = 150;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(
        j, "\"kind\":\"vend_approved\",\"session_id\":\"s1\",\"request_id\":\"req-00000002\",\"cents\":150}"));
    e = make_event(EventKind::VendDenied, Device::Cashless);
    strcpy(e.session_id, "s1");
    strcpy(e.request_id, "req-00000002");
    strcpy(e.detail, "insufficient");
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"vend_denied\",\"session_id\":\"s1\","
                                   "\"request_id\":\"req-00000002\",\"reason\":\"insufficient\"}"));
    e = make_event(EventKind::SessionEnded, Device::Cashless);
    strcpy(e.session_id, "s1");
    e.end_reason = SessionEndReason::Cancelled;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"session_ended\",\"session_id\":\"s1\",\"reason\":\"cancelled\"}"));
    e = make_event(EventKind::DeviceStatus, Device::Coin);
    e.status = Status::Jammed;
    render(e);
    TEST_ASSERT_NOT_NULL(
        strstr(j, "\"kind\":\"device_status\",\"device\":\"coin\",\"status\":\"jammed\",\"mdb_status\":null}"));
    e.mdb_status = 4;
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"mdb_status\":4}"));
    e = make_event(EventKind::CashCollected, Device::Bill);
    render(e);
    TEST_ASSERT_NOT_NULL(strstr(j, "\"kind\":\"cash_collected\",\"device\":\"bill\"}"));
    TEST_ASSERT_EQUAL_UINT(0, event_json(e, "abcdefgh-1", "abcdefgh", 1, "t", j, 10));
}

static void test_peek_rejects_garbage(void) {
    char b[40];
    uint32_t s;
    TEST_ASSERT_FALSE(peek_boot_seq("{}", b, s));
    TEST_ASSERT_FALSE(peek_boot_seq("{\"boot_id\":\"abc\"}", b, s));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_ids_and_envelope);
    RUN_TEST(test_acked_same_boot);
    RUN_TEST(test_two_boots);
    RUN_TEST(test_full_at_256);
    RUN_TEST(test_store_refusal_keeps_seq);
    RUN_TEST(test_too_small_buffer_keeps_seq);
    RUN_TEST(test_event_json_all_kinds);
    RUN_TEST(test_peek_rejects_garbage);
    return UNITY_END();
}
