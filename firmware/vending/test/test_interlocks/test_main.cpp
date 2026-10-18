#include <string.h>
#include <unity.h>

#include "../support/fakes.h"
#include "channels.h"
#include "inputs.h"
#include "interlocks.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static Polarity high_active() {
    Polarity p;
    for (int i = 0; i < kBinaryInputCount; ++i) p.active_low[i] = false;
    return p;
}

constexpr int kAgit = static_cast<int>(Out::Agitator);
constexpr int kValve = static_cast<int>(Out::WaterValve);

static int output_events(const VecPublisher& pub, int channel, bool state) {
    int n = 0;
    for (int i = 0; i < pub.count; ++i) {
        const Event& e = pub.events[i];
        if (e.type == EventType::OutputChanged && e.channel == channel && e.state == state) ++n;
    }
    return n;
}

static void test_max_on_forces_off(void) {
    OutputGuard g;
    VecPublisher pub;
    bool out[kOutputCount] = {};
    g.request(Out::Agitator, true, 500, 0);
    g.apply(0, out, pub);
    TEST_ASSERT_TRUE(out[kAgit]);
    TEST_ASSERT_EQUAL(1, output_events(pub, 9 + kAgit, true));
    g.request(Out::Agitator, true, 500, 400);  // idempotent: does not restart the timer
    g.apply(500, out, pub);
    TEST_ASSERT_TRUE(out[kAgit]);
    g.apply(510, out, pub);
    TEST_ASSERT_FALSE(out[kAgit]);
    TEST_ASSERT_EQUAL(1, output_events(pub, 9 + kAgit, false));
    // stays off until requested off and on again
    g.request(Out::Agitator, true, 500, 520);
    g.apply(520, out, pub);
    TEST_ASSERT_FALSE(out[kAgit]);
    g.request(Out::Agitator, false, 500, 530);
    g.request(Out::Agitator, true, 500, 540);
    g.apply(540, out, pub);
    TEST_ASSERT_TRUE(out[kAgit]);
}

static void test_apply_emits_only_changes(void) {
    OutputGuard g;
    VecPublisher pub;
    bool out[kOutputCount] = {};
    g.request(Out::Heater, true, 100000, 0);
    g.apply(0, out, pub);
    g.apply(10, out, pub);
    TEST_ASSERT_EQUAL(1, pub.count);
}

static void test_all_off(void) {
    OutputGuard g;
    VecPublisher pub;
    bool out[kOutputCount] = {};
    g.request(Out::Agitator, true, 10000, 0);
    g.request(Out::Heater, true, 10000, 0);
    g.apply(0, out, pub);
    g.all_off();
    g.apply(10, out, pub);
    for (int i = 0; i < kOutputCount; ++i) TEST_ASSERT_FALSE(out[i]);
    TEST_ASSERT_FALSE(g.any_on());
}

static void test_any_motion(void) {
    OutputGuard g;
    TEST_ASSERT_FALSE(g.any_motion());
    g.request(Out::Heater, true, 1000, 0);
    TEST_ASSERT_FALSE(g.any_motion());
    TEST_ASSERT_TRUE(g.any_on());
    g.request(Out::WaterValve, true, 1000, 0);
    TEST_ASSERT_TRUE(g.any_motion());
    g.all_off();
    g.request(Out::TrapDoorCatch, true, 1000, 0);
    TEST_ASSERT_TRUE(g.any_motion());
}

static void test_liveness(void) {
    VmcLiveness v;
    TEST_ASSERT_TRUE(v.silent(0));  // never heard
    v.seen(1000);
    TEST_ASSERT_FALSE(v.silent(30999));
    TEST_ASSERT_TRUE(v.silent(31001));
    v.seen(40000);
    TEST_ASSERT_FALSE(v.silent(40001));
    v.set_online(false, 40002);
    TEST_ASSERT_TRUE(v.silent(40002));
    v.set_online(true, 50000);
    TEST_ASSERT_FALSE(v.silent(79999));
    TEST_ASSERT_TRUE(v.silent(80001));
}

struct TripRig {
    InputScanner sc{high_active(), 0};
    VecPublisher pub;
    InputSnapshot raw = {};
    VmcLiveness vmc;
    Interlocks il;
    TripRig() {
        raw.master_enable = true;
        vmc.seen(0);
        sc.scan(raw, 0, pub);
    }
    void set(In i, bool v) {
        raw.binary[static_cast<int>(i)] = v;
        sc.scan(raw, 0, pub);
    }
    // Returns detail or nullptr.
    const char* trip(bool io_ok, bool motion, bool any_on, uint32_t now = 1000) {
        TripReason why = {nullptr};
        return il.trip(sc, raw, io_ok, vmc, motion, any_on, now, why) ? why.detail : nullptr;
    }
};

static void test_clean_state_no_trip(void) {
    TripRig r;
    TEST_ASSERT_NULL(r.trip(true, true, true));
    TEST_ASSERT_NULL(r.trip(true, false, false));
}

static void test_each_rule(void) {
    {
        TripRig r;
        TEST_ASSERT_EQUAL_STRING("io_failure", r.trip(false, false, false));
    }
    {
        TripRig r;
        r.raw.master_enable = false;
        TEST_ASSERT_NULL(r.trip(true, false, false));  // nothing requested on: no trip
        TEST_ASSERT_EQUAL_STRING("master_enable_dropped", r.trip(true, false, true));
    }
    {
        TripRig r;
        r.set(In::SumpHigh, true);
        TEST_ASSERT_EQUAL_STRING("sump_high", r.trip(true, false, false));
    }
    {
        TripRig r;
        r.set(In::ServiceDoor, true);
        TEST_ASSERT_NULL(r.trip(true, false, false));
        TEST_ASSERT_EQUAL_STRING("service_door", r.trip(true, true, true));
    }
    {
        TripRig r;
        TEST_ASSERT_EQUAL_STRING("vmc_offline", r.trip(true, false, false, 30001));
        r.vmc.set_online(false, 100);
        TEST_ASSERT_EQUAL_STRING("vmc_offline", r.trip(true, false, false, 100));
    }
}

static void test_priority(void) {
    TripRig r;
    r.raw.master_enable = false;
    r.set(In::SumpHigh, true);
    r.set(In::ServiceDoor, true);
    TEST_ASSERT_EQUAL_STRING("io_failure", r.trip(false, true, true, 99999));
    TEST_ASSERT_EQUAL_STRING("master_enable_dropped", r.trip(true, true, true, 99999));
    r.raw.master_enable = true;
    TEST_ASSERT_EQUAL_STRING("sump_high", r.trip(true, true, true, 99999));
}

static void test_health(void) {
    Health h;
    TEST_ASSERT_FALSE(h.ok());  // fail-safe boot: no kick until the first tick sets the flags
    h.set_io_ok(true);
    TEST_ASSERT_FALSE(h.ok());
    h.set_deadline_met(true);
    TEST_ASSERT_TRUE(h.ok());
    h.set_io_ok(false);
    TEST_ASSERT_FALSE(h.ok());
    h.set_io_ok(true);
    h.set_deadline_met(false);
    TEST_ASSERT_FALSE(h.ok());
    h.set_deadline_met(true);
    h.set_latched(true);
    TEST_ASSERT_FALSE(h.ok());
    h.set_latched(false);
    TEST_ASSERT_TRUE(h.ok());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_max_on_forces_off);
    RUN_TEST(test_apply_emits_only_changes);
    RUN_TEST(test_all_off);
    RUN_TEST(test_any_motion);
    RUN_TEST(test_liveness);
    RUN_TEST(test_clean_state_no_trip);
    RUN_TEST(test_each_rule);
    RUN_TEST(test_priority);
    RUN_TEST(test_health);
    return UNITY_END();
}
