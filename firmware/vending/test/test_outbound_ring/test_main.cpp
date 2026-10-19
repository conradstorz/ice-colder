#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "outbound_ring.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static void fill(OutboundRing& r, Prio p, int n, const char* tag) {
    for (int i = 0; i < n; ++i) {
        char t[32];
        snprintf(t, sizeof(t), "%s%d", tag, i);
        TEST_ASSERT_TRUE(r.push(t, "x", 1, false, p));
    }
}

static void test_fifo_order(void) {
    OutboundRing r;
    fill(r, Prio::Normal, 3, "n");
    TEST_ASSERT_EQUAL_STRING("n0", r.front()->topic);
    r.pop();
    TEST_ASSERT_EQUAL_STRING("n1", r.front()->topic);
    TEST_ASSERT_EQUAL_INT(2, r.size());
}

static void test_full_drops_telemetry_first(void) {
    OutboundRing r;
    fill(r, Prio::Outcome, 1, "o");
    fill(r, Prio::Telemetry, 2, "t");
    fill(r, Prio::Normal, 61, "n");
    TEST_ASSERT_EQUAL_INT(64, r.size());
    TEST_ASSERT_TRUE(r.push("new", "x", 1, false, Prio::Normal));  // evicts oldest telemetry t0
    TEST_ASSERT_EQUAL_INT(64, r.size());
    TEST_ASSERT_EQUAL_UINT(1, r.dropped());
    TEST_ASSERT_EQUAL_STRING("o0", r.front()->topic);
    r.pop();
    TEST_ASSERT_EQUAL_STRING("t1", r.front()->topic);
}

static void test_outcome_never_dropped_for_lower_prio(void) {
    OutboundRing r;
    fill(r, Prio::Outcome, 64, "o");
    TEST_ASSERT_FALSE(r.push("t", "x", 1, false, Prio::Telemetry));
    TEST_ASSERT_FALSE(r.push("n", "x", 1, false, Prio::Normal));
    TEST_ASSERT_EQUAL_INT(64, r.size());
    for (int i = 0; i < 64; ++i) {
        char t[8];
        snprintf(t, sizeof(t), "o%d", i);
        TEST_ASSERT_EQUAL_STRING(t, r.front()->topic);
        r.pop();
    }
}

static void test_outcome_evicts_normal_before_outcome(void) {
    OutboundRing r;
    fill(r, Prio::Outcome, 63, "o");
    fill(r, Prio::Normal, 1, "n");
    TEST_ASSERT_TRUE(r.push("o63", "x", 1, false, Prio::Outcome));
    int outcomes = 0;
    while (r.front() != nullptr) {
        if (r.front()->prio == Prio::Outcome) ++outcomes;
        r.pop();
    }
    TEST_ASSERT_EQUAL_INT(64, outcomes);
}

static void test_full_reports_capacity(void) {
    OutboundRing r;
    TEST_ASSERT_FALSE(r.full());
    fill(r, Prio::Outcome, OutboundRing::kCapacity - 1, "o");
    TEST_ASSERT_FALSE(r.full());
    fill(r, Prio::Outcome, 1, "last");
    TEST_ASSERT_TRUE(r.full());
    r.pop();
    TEST_ASSERT_FALSE(r.full());
}

static void test_oversize_rejected(void) {
    OutboundRing r;
    char big[300];
    memset(big, 'a', sizeof(big));
    TEST_ASSERT_FALSE(r.push("t", big, sizeof(big), false, Prio::Normal));
    TEST_ASSERT_EQUAL_INT(0, r.size());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_fifo_order);
    RUN_TEST(test_full_drops_telemetry_first);
    RUN_TEST(test_outcome_never_dropped_for_lower_prio);
    RUN_TEST(test_outcome_evicts_normal_before_outcome);
    RUN_TEST(test_full_reports_capacity);
    RUN_TEST(test_oversize_rejected);
    return UNITY_END();
}
