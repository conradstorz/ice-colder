#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "ack_cache.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static Ack make(const char* id, Phase ph = Phase::Accepted) {
    Ack a;
    memset(&a, 0, sizeof(a));
    snprintf(a.request_id, sizeof(a.request_id), "%s", id);
    snprintf(a.command, sizeof(a.command), "ping");
    a.status = AckStatus::Ok;
    a.phase = ph;
    return a;
}

static void test_find_unknown_is_null(void) {
    AckCache c;
    TEST_ASSERT_NULL(c.find("nothing-here"));
    c.put(make("req-0001"));
    TEST_ASSERT_NULL(c.find("req-9999"));
    TEST_ASSERT_NOT_NULL(c.find("req-0001"));
}

static void test_same_id_overwrites_in_place(void) {
    AckCache c;
    c.put(make("req-0001", Phase::Accepted));
    for (int i = 0; i < 31; ++i) {
        char id[16];
        snprintf(id, sizeof(id), "fill-%04d", i);
        c.put(make(id));
    }
    c.put(make("req-0001", Phase::Completed));
    const Ack* a = c.find("req-0001");
    TEST_ASSERT_NOT_NULL(a);
    TEST_ASSERT_TRUE(a->phase == Phase::Completed);
    // Did not grow: all 32 entries are still present.
    TEST_ASSERT_NOT_NULL(c.find("fill-0000"));
    TEST_ASSERT_NOT_NULL(c.find("fill-0030"));
}

static void test_33rd_put_evicts_first(void) {
    AckCache c;
    for (int i = 0; i < 33; ++i) {
        char id[16];
        snprintf(id, sizeof(id), "req-%04d", i);
        c.put(make(id));
    }
    TEST_ASSERT_NULL(c.find("req-0000"));
    TEST_ASSERT_NOT_NULL(c.find("req-0001"));
    TEST_ASSERT_NOT_NULL(c.find("req-0032"));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_find_unknown_is_null);
    RUN_TEST(test_same_id_overwrites_in_place);
    RUN_TEST(test_33rd_put_evicts_first);
    return UNITY_END();
}
