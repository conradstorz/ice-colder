#include <string.h>
#include <unity.h>

#include <ArduinoJson.h>

#include "../fixtures/fixtures.h"
#include "channels.h"
#include "profile.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static ParseResult parse(const char* json, Mechanism m, SlotProfile& p) {
    JsonDocument doc;
    TEST_ASSERT_TRUE(deserializeJson(doc, json) == DeserializationError::Ok);
    return parse_profile(doc.as<JsonVariantConst>(), m, p);
}

static void test_ice_catch(void) {
    SlotProfile p;
    ParseResult r = parse(kFixture_profile_ice_catch, Mechanism::BaggedIce, p);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    TEST_ASSERT_EQUAL_STRING("ice-bag", p.sku);
    TEST_ASSERT_EQUAL(channel_index("agitator_motor"), p.agitate.motor);
    TEST_ASSERT_EQUAL_FLOAT(2.0f, p.agitate.run_s);
    TEST_ASSERT_TRUE(p.ice.proof == IceFill::BySensor);
    TEST_ASSERT_EQUAL(channel_index("auger_motor"), p.ice.motor);
    TEST_ASSERT_EQUAL(channel_index("bag_full_sensor"), p.ice.sensor);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, p.ice.max_run_s);
    TEST_ASSERT_TRUE(p.release.proof == Release::ByCatch);
    TEST_ASSERT_EQUAL(channel_index("trap_door_catch"), p.release.channel);
    TEST_ASSERT_EQUAL(-1, p.release.sensor);
    TEST_ASSERT_EQUAL_FLOAT(3.0f, p.release.drop_s);
    TEST_ASSERT_EQUAL(1, p.acc_count);
    TEST_ASSERT_EQUAL(channel_index("bag_fan"), p.acc[0].channel);
    TEST_ASSERT_EQUAL_UINT8(3, p.acc[0].on_during_mask);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, p.acc[0].lead_s);
    TEST_ASSERT_EQUAL_FLOAT(2.0f, p.acc[0].lag_s);
    // lead + agitate + max fill + drop + lag
    TEST_ASSERT_EQUAL_FLOAT(1.0f + 2.0f + 20.0f + 3.0f + 2.0f, worst_case_seconds(p));
}

static void test_ice_timed(void) {
    SlotProfile p;
    ParseResult r = parse(kFixture_profile_ice_timed, Mechanism::BaggedIce, p);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    TEST_ASSERT_TRUE(p.ice.proof == IceFill::Timed);
    TEST_ASSERT_EQUAL(-1, p.ice.sensor);
    TEST_ASSERT_TRUE(p.release.proof == Release::Timed);
    TEST_ASSERT_EQUAL_FLOAT(1.5f, p.release.pulse_s);
    TEST_ASSERT_EQUAL(0, p.acc_count);
    TEST_ASSERT_EQUAL_FLOAT(2.0f + 10.0f + 1.5f, worst_case_seconds(p));
}

static void test_water_1gal(void) {
    SlotProfile p;
    ParseResult r = parse(kFixture_profile_water_1gal, Mechanism::WaterFill, p);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    TEST_ASSERT_TRUE(p.water.proof == WaterFill::ByVolume);
    TEST_ASSERT_EQUAL(channel_index("water_valve_solenoid"), p.water.valve);
    TEST_ASSERT_EQUAL(channel_index("fill_pulses"), p.water.flow);
    TEST_ASSERT_EQUAL_FLOAT(3785.0f, p.water.target_ml);
    TEST_ASSERT_EQUAL_FLOAT(450.0f, p.water.pulses_per_l);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, p.water.min_flow_ml_s);
    TEST_ASSERT_EQUAL_FLOAT(3.0f, p.water.no_flow_grace_s);
    TEST_ASSERT_EQUAL_FLOAT(5.0f, p.water.over_pct);
    TEST_ASSERT_EQUAL_FLOAT(120.0f, p.water.max_fill_s);
    TEST_ASSERT_EQUAL_FLOAT(123.0f, worst_case_seconds(p));
}

static void test_water_timed(void) {
    SlotProfile p;
    ParseResult r = parse(kFixture_profile_water_timed, Mechanism::WaterFill, p);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    TEST_ASSERT_TRUE(p.water.proof == WaterFill::Timed);
    TEST_ASSERT_EQUAL(-1, p.water.flow);
    TEST_ASSERT_EQUAL_FLOAT(30.0f, worst_case_seconds(p));
}

static void expect_fail(const char* json, Mechanism m, const char* prefix) {
    SlotProfile p;
    ParseResult r = parse(json, m, p);
    TEST_ASSERT_FALSE(r.ok);
    TEST_ASSERT_EQUAL_STRING_LEN(prefix, r.detail, strlen(prefix));
}

static void test_bad_mechanism(void) {
    expect_fail(kFixture_bad_mechanism, Mechanism::BaggedIce, "mechanism mismatch");
}
static void test_bad_channel(void) {
    expect_fail(kFixture_bad_channel, Mechanism::BaggedIce, "undeclared channel water_flow");
}
static void test_bad_range(void) {
    expect_fail(kFixture_bad_range, Mechanism::BaggedIce, "agitate.run_seconds out of range");
}
static void test_bad_extra_key(void) {
    expect_fail(kFixture_bad_extra_key, Mechanism::BaggedIce, "unknown key bogus");
}
static void test_wrong_direction(void) {
    expect_fail(
        R"({"mechanism":"water_fill","product_sku":"w","fill":{"proof":"timed","valve_channel":"sump_high","max_fill_seconds":5}})",
        Mechanism::WaterFill, "fill.valve_channel wrong direction");
}
static void test_accessory_step_for_wrong_mechanism(void) {
    expect_fail(
        R"({"mechanism":"water_fill","product_sku":"w","fill":{"proof":"timed","valve_channel":"water_valve_solenoid","max_fill_seconds":5},"accessories":{"f":{"channel":"bag_fan","on_during":["agitate"],"lead_seconds":0,"lag_seconds":0}}})",
        Mechanism::WaterFill, "accessories.f.on_during invalid");
}
static void test_accessory_gap(void) {
    expect_fail(
        R"({"mechanism":"bagged_ice","product_sku":"i","agitate":{"motor_channel":"agitator_motor","run_seconds":2,"stall_current_amps":"unmonitored","current_channel":"unmonitored"},"fill":{"proof":"timed","motor_channel":"auger_motor","max_run_seconds":5,"stall_current_amps":"unmonitored","current_channel":"unmonitored"},"release":{"proof":"catch","catch_channel":"trap_door_catch","drop_seconds":1},"accessories":{"f":{"channel":"bag_fan","on_during":["agitate","release"],"lead_seconds":0,"lag_seconds":0}}})",
        Mechanism::BaggedIce, "accessories.f.on_during invalid");
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_ice_catch);
    RUN_TEST(test_ice_timed);
    RUN_TEST(test_water_1gal);
    RUN_TEST(test_water_timed);
    RUN_TEST(test_bad_mechanism);
    RUN_TEST(test_bad_channel);
    RUN_TEST(test_bad_range);
    RUN_TEST(test_bad_extra_key);
    RUN_TEST(test_wrong_direction);
    RUN_TEST(test_accessory_step_for_wrong_mechanism);
    RUN_TEST(test_accessory_gap);
    return UNITY_END();
}
