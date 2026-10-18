#include <unity.h>

#include "../support/fakes.h"
#include "channels.h"
#include "inputs.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static Polarity high_active() {
    Polarity p;
    for (int i = 0; i < kBinaryInputCount; ++i) p.active_low[i] = false;
    return p;
}

static InputSnapshot snap() {
    InputSnapshot s = {};
    return s;
}

static void set_in(InputSnapshot& s, In w, bool v) { s.binary[static_cast<int>(w)] = v; }

static void test_blip_emits_nothing(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    sc.scan(s, 0, pub);
    set_in(s, In::ServiceDoor, true);
    sc.scan(s, 10, pub);
    sc.scan(s, 20, pub);
    set_in(s, In::ServiceDoor, false);
    sc.scan(s, 30, pub);
    sc.scan(s, 40, pub);
    sc.scan(s, 50, pub);
    sc.scan(s, 60, pub);
    TEST_ASSERT_EQUAL(0, pub.count);
    TEST_ASSERT_FALSE(sc.state(In::ServiceDoor));
}

static void test_hold_emits_one_change(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    sc.scan(s, 0, pub);
    set_in(s, In::ServiceDoor, true);
    for (uint32_t t = 10; t <= 50; t += 10) sc.scan(s, t, pub);
    TEST_ASSERT_EQUAL(1, pub.count);
    TEST_ASSERT_TRUE(pub.events[0].type == EventType::InputChanged);
    TEST_ASSERT_EQUAL(channel_index("service_door"), pub.events[0].channel);
    TEST_ASSERT_TRUE(pub.events[0].state);
    TEST_ASSERT_TRUE(sc.state(In::ServiceDoor));
}

static void test_active_low_inverts(void) {
    Polarity p = high_active();
    p.active_low[static_cast<int>(In::SumpHigh)] = true;
    InputScanner sc(p, 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    s.binary[static_cast<int>(In::SumpHigh)] = true;  // raw 1 = not asserted
    sc.scan(s, 0, pub);
    TEST_ASSERT_FALSE(sc.state(In::SumpHigh));
    s.binary[static_cast<int>(In::SumpHigh)] = false;  // raw 0 = asserted
    for (uint32_t t = 10; t <= 50; t += 10) sc.scan(s, t, pub);
    TEST_ASSERT_TRUE(sc.state(In::SumpHigh));
    TEST_ASSERT_EQUAL(1, pub.count);
    TEST_ASSERT_TRUE(pub.events[0].state);
}

static void test_button_pressed_once_per_edge(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    sc.scan(s, 0, pub);
    set_in(s, In::ButtonIce, true);
    uint32_t t = 10;
    for (; t <= 100; t += 10) sc.scan(s, t, pub);
    int presses = 0, changes = 0;
    for (int i = 0; i < pub.count; ++i) {
        if (pub.events[i].type == EventType::ButtonPressed) {
            ++presses;
            TEST_ASSERT_EQUAL(channel_index("button_ice"), pub.events[i].channel);
        }
        if (pub.events[i].type == EventType::InputChanged) ++changes;
    }
    TEST_ASSERT_EQUAL(1, presses);
    TEST_ASSERT_EQUAL(1, changes);
    // release, then press again
    set_in(s, In::ButtonIce, false);
    for (int i = 0; i < 5; ++i, t += 10) sc.scan(s, t, pub);
    set_in(s, In::ButtonIce, true);
    for (int i = 0; i < 5; ++i, t += 10) sc.scan(s, t, pub);
    presses = 0;
    for (int i = 0; i < pub.count; ++i)
        if (pub.events[i].type == EventType::ButtonPressed) ++presses;
    TEST_ASSERT_EQUAL(2, presses);
}

static void test_non_button_no_press(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    sc.scan(s, 0, pub);
    set_in(s, In::BagFull, true);
    for (uint32_t t = 10; t <= 60; t += 10) sc.scan(s, t, pub);
    for (int i = 0; i < pub.count; ++i) TEST_ASSERT_TRUE(pub.events[i].type == EventType::InputChanged);
}

static void test_republish_all(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    set_in(s, In::RoTankFull, true);
    sc.scan(s, 0, pub);
    TEST_ASSERT_EQUAL(0, pub.count);
    sc.republish_all(pub);
    TEST_ASSERT_EQUAL(9, pub.count);
    for (int i = 0; i < 9; ++i) {
        TEST_ASSERT_TRUE(pub.events[i].type == EventType::InputChanged);
        TEST_ASSERT_EQUAL(i, pub.events[i].channel);
    }
    TEST_ASSERT_TRUE(pub.events[static_cast<int>(In::RoTankFull)].state);
}

static void test_pulses_tracks_raw(void) {
    InputScanner sc(high_active(), 30);
    VecPublisher pub;
    InputSnapshot s = snap();
    s.pulses = 42;
    sc.scan(s, 0, pub);
    TEST_ASSERT_EQUAL_UINT32(42, sc.pulses());
    s.pulses = 99;
    sc.scan(s, 10, pub);
    TEST_ASSERT_EQUAL_UINT32(99, sc.pulses());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_blip_emits_nothing);
    RUN_TEST(test_hold_emits_one_change);
    RUN_TEST(test_active_low_inverts);
    RUN_TEST(test_button_pressed_once_per_edge);
    RUN_TEST(test_non_button_no_press);
    RUN_TEST(test_republish_all);
    RUN_TEST(test_pulses_tracks_raw);
    return UNITY_END();
}
