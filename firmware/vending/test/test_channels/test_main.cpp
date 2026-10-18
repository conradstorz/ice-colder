#include <string.h>
#include <unity.h>

#include "channels.h"
#include "ports.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static const char* const kIds[18] = {
    "bag_full_sensor", "button_ice", "button_water_1gal", "button_water_5gal", "ro_tank_full",
    "ro_tank_empty", "water_pressure_ok", "sump_high", "service_door", "agitator_motor", "auger_motor",
    "bag_fan", "trap_door_catch", "water_valve_solenoid", "ro_production", "heater_power", "fill_pulses",
    "ambient"};

static void test_table_ids_and_order(void) {
    TEST_ASSERT_EQUAL(18, kChannelCount);
    for (int i = 0; i < 18; ++i) TEST_ASSERT_EQUAL_STRING(kIds[i], kChannels[i].id);
}

static void test_driven_by(void) {
    for (int i = 9; i <= 12; ++i) TEST_ASSERT_EQUAL_STRING("dispense", kChannels[i].driven_by);
    TEST_ASSERT_EQUAL_STRING("water_valve", kChannels[13].driven_by);
    TEST_ASSERT_NULL(kChannels[14].driven_by);
    TEST_ASSERT_NULL(kChannels[15].driven_by);
    for (int i = 0; i < 9; ++i) TEST_ASSERT_NULL(kChannels[i].driven_by);
}

static void test_directions_and_kinds(void) {
    for (int i = 0; i < 9; ++i) {
        TEST_ASSERT_TRUE(kChannels[i].dir == Dir::Input);
        TEST_ASSERT_TRUE(kChannels[i].kind == Kind::Binary);
    }
    for (int i = 9; i < 16; ++i) TEST_ASSERT_TRUE(kChannels[i].dir == Dir::Output);
    TEST_ASSERT_TRUE(kChannels[16].kind == Kind::Counter);
    TEST_ASSERT_TRUE(kChannels[17].kind == Kind::Temperature);
    TEST_ASSERT_EQUAL_STRING("pulses", kChannels[16].unit);
    TEST_ASSERT_EQUAL_STRING("C", kChannels[17].unit);
    TEST_ASSERT_EQUAL_FLOAT(10.0f, kChannels[17].interval_s);
}

static void test_round_trip(void) {
    for (int i = 0; i < 18; ++i) TEST_ASSERT_EQUAL(i, channel_index(kChannels[i].id));
    TEST_ASSERT_EQUAL(-1, channel_index("water_flow"));
    TEST_ASSERT_EQUAL(-1, channel_index(nullptr));
}

static void test_classification(void) {
    TEST_ASSERT_TRUE(is_binary_input(0));
    TEST_ASSERT_TRUE(is_binary_input(8));
    TEST_ASSERT_FALSE(is_binary_input(9));
    TEST_ASSERT_TRUE(is_output(9));
    TEST_ASSERT_TRUE(is_output(15));
    TEST_ASSERT_FALSE(is_output(16));
    TEST_ASSERT_FALSE(is_output(0));
    TEST_ASSERT_EQUAL(static_cast<int>(Out::Agitator), static_cast<int>(out_from_index(9)));
    TEST_ASSERT_EQUAL(static_cast<int>(Out::Heater), static_cast<int>(out_from_index(15)));
    TEST_ASSERT_EQUAL(static_cast<int>(In::ServiceDoor), static_cast<int>(in_from_index(8)));
    TEST_ASSERT_EQUAL_STRING("vending", kSubsystem);
    TEST_ASSERT_EQUAL_STRING("water_valve", kCommands[4]);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_table_ids_and_order);
    RUN_TEST(test_driven_by);
    RUN_TEST(test_directions_and_kinds);
    RUN_TEST(test_round_trip);
    RUN_TEST(test_classification);
    return UNITY_END();
}
