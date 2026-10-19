#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "../support/fakes.h"
#include "accounts.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

static void test_arithmetic_and_clamp(void) {
    Accounts a;
    a.coin(25);
    a.coin(100);
    a.bill_escrowed(500);
    uint32_t b = 0;
    TEST_ASSERT_TRUE(a.bill_in_escrow(b));
    TEST_ASSERT_EQUAL_UINT32(500, b);
    TEST_ASSERT_EQUAL_UINT32(125, a.escrow_cents());  // escrowed bill is not credit yet
    a.bill_stacked(500);
    TEST_ASSERT_FALSE(a.bill_in_escrow(b));
    TEST_ASSERT_EQUAL_UINT32(625, a.escrow_cents());
    TEST_ASSERT_TRUE(a.cash_sale(200));
    TEST_ASSERT_EQUAL_UINT32(425, a.escrow_cents());
    TEST_ASSERT_FALSE(a.cash_sale(1000));  // clamped
    TEST_ASSERT_EQUAL_UINT32(0, a.escrow_cents());
    a.bill_escrowed(100);
    a.bill_escrow_cleared();
    TEST_ASSERT_FALSE(a.bill_in_escrow(b));
}

static void test_would_exceed(void) {
    Accounts a;
    a.coin(1500);
    TEST_ASSERT_FALSE(a.would_exceed(500, 2000));
    TEST_ASSERT_TRUE(a.would_exceed(501, 2000));
    TEST_ASSERT_TRUE(a.would_exceed(0xFFFFFFFFu, 0xFFFFFFFFu));  // no 32-bit wrap
}

static void test_payout_remember_find(void) {
    Accounts a;
    TEST_ASSERT_TRUE(a.remember_payout("req-00000001", PayoutStatus::Partial, 300, 250));
    TEST_ASSERT_FALSE(a.remember_payout("req-00000001", PayoutStatus::Ok, 300, 300));
    PaymentEvent e;
    TEST_ASSERT_TRUE(a.find_payout("req-00000001", e));
    TEST_ASSERT_EQUAL(static_cast<int>(EventKind::PayoutResult), static_cast<int>(e.kind));
    TEST_ASSERT_EQUAL(static_cast<int>(PayoutStatus::Partial), static_cast<int>(e.payout_status));
    TEST_ASSERT_EQUAL_UINT32(300, e.requested_cents);
    TEST_ASSERT_EQUAL_UINT32(250, e.paid_cents);
    TEST_ASSERT_EQUAL_STRING("req-00000001", e.request_id);
    TEST_ASSERT_FALSE(a.find_payout("nope", e));
}

static void id(char* out, int i) { snprintf(out, 65, "req-%08d", i); }

static void test_payout_eviction(void) {
    Accounts a;
    char r[65];
    for (int i = 0; i < 33; ++i) {
        id(r, i);
        TEST_ASSERT_TRUE(a.remember_payout(r, PayoutStatus::Ok, 100, 100));
    }
    TEST_ASSERT_EQUAL_INT(32, a.payout_count());
    PaymentEvent e;
    id(r, 0);
    TEST_ASSERT_FALSE(a.find_payout(r, e));
    id(r, 1);
    TEST_ASSERT_TRUE(a.find_payout(r, e));
    id(r, 32);
    TEST_ASSERT_TRUE(a.find_payout(r, e));
}

static void test_json_round_trip(void) {
    Accounts a;
    char r[65];
    for (int i = 0; i < 40; ++i) {  // wraps the ring
        id(r, i);
        a.remember_payout(r, static_cast<PayoutStatus>(i % 3), 100 + i, 50 + i);
    }
    static char js[4096];
    const size_t n = a.payouts_json(js, sizeof(js));
    TEST_ASSERT_GREATER_THAN_UINT(0, n);
    TEST_ASSERT_EQUAL_CHAR('[', js[0]);
    Accounts b;
    b.restore_payouts(js, n);
    TEST_ASSERT_EQUAL_INT(32, b.payout_count());
    static char js2[4096];
    TEST_ASSERT_EQUAL_UINT(n, b.payouts_json(js2, sizeof(js2)));
    TEST_ASSERT_EQUAL_STRING(js, js2);
    PaymentEvent e;
    id(r, 38);  // 38 % 3 == 2 -> Failed
    TEST_ASSERT_TRUE(b.find_payout(r, e));
    TEST_ASSERT_EQUAL_UINT32(138, e.requested_cents);
    TEST_ASSERT_EQUAL_UINT32(88, e.paid_cents);
    TEST_ASSERT_EQUAL(static_cast<int>(PayoutStatus::Failed), static_cast<int>(e.payout_status));
}

static void test_json_empty_and_garbage(void) {
    Accounts a;
    char js[8];
    TEST_ASSERT_EQUAL_UINT(2, a.payouts_json(js, sizeof(js)));
    TEST_ASSERT_EQUAL_STRING("[]", js);
    TEST_ASSERT_EQUAL_UINT(0, a.payouts_json(js, 1));  // does not fit
    a.remember_payout("req-00000001", PayoutStatus::Ok, 1, 1);
    a.restore_payouts("not json", 8);
    TEST_ASSERT_EQUAL_INT(0, a.payout_count());
    const char* bad = "[{\"request_id\":\"req-00000001\",\"status\":\"zzz\",\"requested\":1,\"paid\":1}]";
    a.restore_payouts(bad, strlen(bad));
    TEST_ASSERT_EQUAL_INT(0, a.payout_count());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_arithmetic_and_clamp);
    RUN_TEST(test_would_exceed);
    RUN_TEST(test_payout_remember_find);
    RUN_TEST(test_payout_eviction);
    RUN_TEST(test_json_round_trip);
    RUN_TEST(test_json_empty_and_garbage);
    return UNITY_END();
}
