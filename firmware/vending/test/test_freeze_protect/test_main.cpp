#include <unity.h>

#include "../support/fakes.h"
#include "channels.h"
#include "freeze_protect.h"
#include "interlocks.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

constexpr int kHeat = static_cast<int>(Out::Heater);

struct Rig {
    OutputGuard guard;
    FreezeProtect fp;
    VecPublisher pub;
    bool out[kOutputCount] = {};
    uint32_t t = 0;

    bool step(float c, bool valid = true) {
        InputSnapshot raw = {};
        raw.ambient_c = c;
        raw.ambient_valid = valid;
        t += 1000;
        fp.tick(raw, guard, t);
        guard.apply(t, out, pub);
        return out[kHeat];
    }
};

static void test_hysteresis(void) {
    Rig r;
    TEST_ASSERT_FALSE(r.step(10.0f));
    TEST_ASSERT_TRUE(r.step(2.9f));
    TEST_ASSERT_TRUE(r.step(4.5f));
    TEST_ASSERT_FALSE(r.step(6.1f));
    TEST_ASSERT_FALSE(r.step(4.5f));
}

static void test_invalid_sensor_forces_off(void) {
    Rig r;
    TEST_ASSERT_TRUE(r.step(2.0f));
    TEST_ASSERT_FALSE(r.step(2.0f, false));
    TEST_ASSERT_FALSE(r.step(4.5f));  // stays off in the dead band after recovery
}

// Continuous cold: on for 24 h, off for exactly 5 min, then on again; the cycle repeats. The guard's
// own max-on bound never fires first (it would latch the heater off until the ambient warmed).
static void test_duty_cycle_after_24h_of_cold(void) {
    Rig r;
    constexpr int kOnS = 86400, kRestS = 300;
    for (int cycle = 0; cycle < 2; ++cycle) {
        // Cycle 1's first on step was already consumed by the rest loop below, hence kOnS - 1.
        int on_steps = 0;
        while (r.step(1.0f)) ++on_steps;  // first false step is the first rest step
        TEST_ASSERT_EQUAL(cycle == 0 ? kOnS : kOnS - 1, on_steps);
        int off_steps = 1;
        while (!r.step(1.0f)) ++off_steps;
        TEST_ASSERT_EQUAL(kRestS, off_steps);
    }
}

static void test_hold_off_restarts_the_on_clock(void) {
    Rig r;
    TEST_ASSERT_TRUE(r.step(1.0f));
    r.t += 1000;
    r.fp.hold_off(r.guard, r.t);
    r.guard.apply(r.t, r.out, r.pub);
    TEST_ASSERT_FALSE(r.out[kHeat]);
    TEST_ASSERT_TRUE(r.step(1.0f));  // cold still wanted: back on once the interlock releases
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_hysteresis);
    RUN_TEST(test_invalid_sensor_forces_off);
    RUN_TEST(test_duty_cycle_after_24h_of_cold);
    RUN_TEST(test_hold_off_restarts_the_on_clock);
    return UNITY_END();
}
