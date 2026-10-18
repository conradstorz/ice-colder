#include <stdio.h>
#include <string.h>
#include <unity.h>

#include <string>

#include "../fixtures/fixtures.h"
#include "command.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static Decoded dec(const char* j) { return decode_command(j, strlen(j)); }

static void test_request_id_validation(void) {
    TEST_ASSERT_TRUE(dec(R"({"command":"ping"})").result == Decode::NoRequestId);
    TEST_ASSERT_TRUE(dec(R"({"command":"ping","request_id":""})").result == Decode::NoRequestId);
    TEST_ASSERT_TRUE(dec(R"({"command":"ping","request_id":12345678})").result == Decode::NoRequestId);
    TEST_ASSERT_TRUE(dec(R"({"command":"ping","request_id":"1234567"})").result == Decode::NoRequestId);
    std::string s65 = R"({"command":"ping","request_id":")" + std::string(65, 'a') + R"("})";
    TEST_ASSERT_TRUE(dec(s65.c_str()).result == Decode::NoRequestId);
    std::string s64 = R"({"command":"ping","request_id":")" + std::string(64, 'a') + R"("})";
    TEST_ASSERT_TRUE(dec(s64.c_str()).result == Decode::Ok);
    Decoded ok = dec(R"({"command":"ping","request_id":"12345678"})");
    TEST_ASSERT_TRUE(ok.result == Decode::Ok);
    TEST_ASSERT_TRUE(ok.cmd.kind == CmdKind::Ping);
}

static void test_unsupported_keeps_id(void) {
    Decoded d = dec(R"({"command":"frobnicate","request_id":"abcdefgh"})");
    TEST_ASSERT_TRUE(d.result == Decode::Unsupported);
    TEST_ASSERT_EQUAL_STRING("abcdefgh", d.cmd.request_id);
}

static void test_standard_kinds(void) {
    TEST_ASSERT_TRUE(dec(R"({"command":"self_test","request_id":"abcdefgh"})").cmd.kind == CmdKind::SelfTest);
    TEST_ASSERT_TRUE(dec(R"({"command":"force_report","request_id":"abcdefgh","params":{}})").cmd.kind ==
                     CmdKind::ForceReport);
}

static void test_water_valve(void) {
    Decoded lo = dec(R"({"command":"water_valve","request_id":"abcdefgh","params":{"seconds":0}})");
    TEST_ASSERT_TRUE(lo.result == Decode::Rejected);
    TEST_ASSERT_EQUAL_STRING("seconds out of range [1, 10]", lo.detail);
    Decoded hi = dec(R"({"command":"water_valve","request_id":"abcdefgh","params":{"seconds":11}})");
    TEST_ASSERT_TRUE(hi.result == Decode::Rejected);
    TEST_ASSERT_EQUAL_STRING("seconds out of range [1, 10]", hi.detail);
    Decoded a = dec(R"({"command":"water_valve","request_id":"abcdefgh","params":{"seconds":1}})");
    TEST_ASSERT_TRUE(a.result == Decode::Ok);
    TEST_ASSERT_EQUAL_UINT8(1, a.cmd.valve_seconds);
    Decoded b = dec(R"({"command":"water_valve","request_id":"abcdefgh","params":{"seconds":10}})");
    TEST_ASSERT_TRUE(b.result == Decode::Ok);
    TEST_ASSERT_EQUAL_UINT8(10, b.cmd.valve_seconds);
    TEST_ASSERT_TRUE(dec(R"({"command":"water_valve","request_id":"abcdefgh"})").result == Decode::Rejected);
}

static Decoded dispense(const char* slot, const char* mech, const char* profile) {
    std::string j = std::string(R"({"command":"dispense","request_id":"abcdefgh","params":{"slot":)") + slot +
                    R"(,"mechanism":")" + mech + R"(","profile":)" + profile + "}}";
    return dec(j.c_str());
}

static void test_dispense_ok(void) {
    Decoded i = dispense("2", "bagged_ice", kFixture_profile_ice_catch);
    TEST_ASSERT_TRUE_MESSAGE(i.result == Decode::Ok, i.detail);
    TEST_ASSERT_TRUE(i.cmd.kind == CmdKind::Dispense);
    TEST_ASSERT_EQUAL_UINT8(2, i.cmd.slot);
    TEST_ASSERT_TRUE(i.cmd.mechanism == Mechanism::BaggedIce);
    TEST_ASSERT_EQUAL_STRING("ice-bag", i.cmd.profile.sku);
    Decoded w = dispense("1", "water_fill", kFixture_profile_water_1gal);
    TEST_ASSERT_TRUE_MESSAGE(w.result == Decode::Ok, w.detail);
    TEST_ASSERT_EQUAL_UINT8(1, w.cmd.slot);
    TEST_ASSERT_TRUE(w.cmd.mechanism == Mechanism::WaterFill);
    TEST_ASSERT_EQUAL_STRING("water-1gal", w.cmd.profile.sku);
}

static void test_dispense_missing(void) {
    Decoded d = dec(R"({"command":"dispense","request_id":"abcdefgh","params":{"slot":1,"mechanism":"water_fill"}})");
    TEST_ASSERT_TRUE(d.result == Decode::Rejected);
    TEST_ASSERT_EQUAL_STRING("dispense requires: slot, mechanism, profile (missing: profile)", d.detail);
    Decoded e = dec(R"({"command":"dispense","request_id":"abcdefgh"})");
    TEST_ASSERT_TRUE(e.result == Decode::Rejected);
    TEST_ASSERT_EQUAL_STRING("dispense requires: slot, mechanism, profile (missing: slot, mechanism, profile)",
                             e.detail);
}

static void test_dispense_invalid(void) {
    TEST_ASSERT_TRUE(dispense("-1", "bagged_ice", kFixture_profile_ice_catch).result == Decode::Rejected);
    TEST_ASSERT_TRUE(dispense("1", "plasma", kFixture_profile_ice_catch).result == Decode::Rejected);
    // Mechanism disagrees with the profile document.
    Decoded mm = dispense("1", "water_fill", kFixture_profile_ice_catch);
    TEST_ASSERT_TRUE(mm.result == Decode::Rejected);
    TEST_ASSERT_EQUAL_STRING("mechanism mismatch", mm.detail);
    TEST_ASSERT_TRUE(dispense("1", "bagged_ice", kFixture_bad_channel).result == Decode::Rejected);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_request_id_validation);
    RUN_TEST(test_unsupported_keeps_id);
    RUN_TEST(test_standard_kinds);
    RUN_TEST(test_water_valve);
    RUN_TEST(test_dispense_ok);
    RUN_TEST(test_dispense_missing);
    RUN_TEST(test_dispense_invalid);
    return UNITY_END();
}
