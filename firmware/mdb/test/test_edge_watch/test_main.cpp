#include <unity.h>

#include "../support/fakes.h"
#include "edge_watch.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

static void test_expiry_and_seen(void) {
    FakeClock c;
    EdgeWatch w(c);
    w.set_timeout_ms(30000);
    c.advance_ms(29999);
    TEST_ASSERT_FALSE(w.expired());
    c.advance_ms(1);
    TEST_ASSERT_TRUE(w.expired());
    w.seen();
    TEST_ASSERT_FALSE(w.expired());
    c.advance_ms(30000);
    TEST_ASSERT_TRUE(w.expired());
}

static void test_single_silence_edge(void) {
    FakeClock c;
    EdgeWatch w(c);
    w.set_timeout_ms(1000);
    TEST_ASSERT_FALSE(w.take_silence_edge());
    c.advance_ms(1000);
    TEST_ASSERT_TRUE(w.take_silence_edge());
    TEST_ASSERT_FALSE(w.take_silence_edge());
    TEST_ASSERT_TRUE(w.expired());  // still silent
    w.seen();
    c.advance_ms(1000);
    TEST_ASSERT_TRUE(w.take_silence_edge());  // new onset
}

static void test_zero_timeout_never_expires(void) {
    FakeClock c;
    EdgeWatch w(c);
    w.set_timeout_ms(0);
    c.advance_ms(1000000);
    TEST_ASSERT_FALSE(w.expired());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_expiry_and_seen);
    RUN_TEST(test_single_silence_edge);
    RUN_TEST(test_zero_timeout_never_expires);
    return UNITY_END();
}
