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

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_hysteresis);
    RUN_TEST(test_invalid_sensor_forces_off);
    return UNITY_END();
}
