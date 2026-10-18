#include <string.h>
#include <unity.h>

#include "../support/fakes.h"
#include "channels.h"
#include "inputs.h"
#include "interlocks.h"
#include "ro_loop.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

constexpr int kRo = static_cast<int>(Out::RoProduction);

struct Rig {
    Polarity pol = {};
    InputScanner sc{pol, 0};
    VecPublisher pub;
    OutputGuard guard;
    RoLoop ro;
    InputSnapshot raw = {};
    bool out[kOutputCount] = {};

    void set_full(bool v, uint32_t now) {
        raw.binary[static_cast<int>(In::RoTankFull)] = v;
        sc.scan(raw, now, pub);
    }
    bool step(uint32_t now) {
        ro.tick(sc, guard, now, pub);
        guard.apply(now, out, pub);
        return out[kRo];
    }
    int health_events() const {
        int n = 0;
        for (int i = 0; i < pub.count; ++i)
            if (pub.events[i].type == EventType::Health && strcmp(pub.events[i].text, "ro_overrun_latched") == 0) ++n;
        return n;
    }
};

static void test_pump_follows_tank_full(void) {
    Rig r;
    r.set_full(false, 0);
    TEST_ASSERT_TRUE(r.step(0));
    r.set_full(true, 10);
    TEST_ASSERT_FALSE(r.step(10));
    r.set_full(false, 20);
    TEST_ASSERT_TRUE(r.step(20));
}

static void test_overrun_latches_and_releases(void) {
    Rig r;
    r.set_full(false, 0);
    TEST_ASSERT_TRUE(r.step(0));
    TEST_ASSERT_TRUE(r.step(5400000));
    TEST_ASSERT_FALSE(r.ro.latched());
    TEST_ASSERT_FALSE(r.step(5400001));
    TEST_ASSERT_TRUE(r.ro.latched());
    TEST_ASSERT_EQUAL(1, r.health_events());
    TEST_ASSERT_FALSE(r.step(5400100));
    TEST_ASSERT_FALSE(r.step(6000000));
    TEST_ASSERT_EQUAL(1, r.health_events());
    r.set_full(true, 6000001);
    TEST_ASSERT_FALSE(r.step(6000001));
    TEST_ASSERT_FALSE(r.ro.latched());
    r.set_full(false, 6000100);
    TEST_ASSERT_TRUE(r.step(6000100));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_pump_follows_tank_full);
    RUN_TEST(test_overrun_latches_and_releases);
    return UNITY_END();
}
