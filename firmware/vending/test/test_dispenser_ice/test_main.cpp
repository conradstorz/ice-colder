#include <string.h>
#include <unity.h>

#include <ArduinoJson.h>

#include "../fixtures/fixtures.h"
#include "../support/fakes.h"
#include "channels.h"
#include "command.h"
#include "dispenser.h"
#include "inputs.h"
#include "interlocks.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

constexpr int kCatch = static_cast<int>(Out::TrapDoorCatch);
constexpr int kAgit = static_cast<int>(Out::Agitator);
constexpr int kAuger = static_cast<int>(Out::Auger);
constexpr int kFan = static_cast<int>(Out::BagFan);

static Polarity high_active() {
    Polarity p;
    for (int i = 0; i < kBinaryInputCount; ++i) p.active_low[i] = false;
    return p;
}

struct Rig {
    Dispenser d;
    InputScanner sc{high_active(), 0};
    VecPublisher pub;
    InputSnapshot raw = {};
    OutputGuard guard;
    bool out[kOutputCount] = {};
    uint32_t t = 0;
    // First tick time each output was seen on / last seen turning off (-1 = never).
    int32_t on_at[kOutputCount];
    int32_t off_at[kOutputCount];
    bool prev[kOutputCount] = {};

    Rig() {
        for (int i = 0; i < kOutputCount; ++i) on_at[i] = off_at[i] = -1;
    }
    void tick() {
        sc.scan(raw, t, pub);
        d.tick(t, sc, guard, pub);
        guard.apply(t, out, pub);
        for (int i = 0; i < kOutputCount; ++i) {
            if (out[i] && !prev[i] && on_at[i] < 0) on_at[i] = (int32_t)t;
            if (!out[i] && prev[i]) off_at[i] = (int32_t)t;
            prev[i] = out[i];
        }
        t += 10;
    }
    // Tick every 10 ms through time `until` inclusive.
    void run_to(uint32_t until) {
        while (t <= until) tick();
    }
    void set_bag_full(bool v) { raw.binary[static_cast<int>(In::BagFull)] = v; }
    int outcomes() const {
        int n = 0;
        for (int i = 0; i < pub.count; ++i) n += pub.events[i].type == EventType::Outcome;
        return n;
    }
    const Event* outcome() const {
        for (int i = 0; i < pub.count; ++i)
            if (pub.events[i].type == EventType::Outcome) return &pub.events[i];
        return nullptr;
    }
    bool all_off() const {
        for (int i = 0; i < kOutputCount; ++i)
            if (out[i]) return false;
        return true;
    }
};

static Command make_cmd(const char* fixture) {
    static JsonDocument doc;
    doc.clear();
    deserializeJson(doc, fixture);
    Command c = {};
    c.kind = CmdKind::Dispense;
    strcpy(c.request_id, "req-1");
    c.slot = 3;
    c.mechanism = Mechanism::BaggedIce;
    ParseResult r = parse_profile(doc.as<JsonVariantConst>(), Mechanism::BaggedIce, c.profile);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    return c;
}

static void test_names(void) {
    TEST_ASSERT_EQUAL_STRING("agitate", step_name(Step::Agitate));
    TEST_ASSERT_EQUAL_STRING("fill", step_name(Step::Fill));
    TEST_ASSERT_EQUAL_STRING("release", step_name(Step::Release));
    TEST_ASSERT_EQUAL_STRING("complete", outcome_name(Outcome::Complete));
    TEST_ASSERT_EQUAL_STRING("bin_empty", outcome_name(Outcome::BinEmpty));
    TEST_ASSERT_EQUAL_STRING("timeout", outcome_name(Outcome::Timeout));
    TEST_ASSERT_EQUAL_STRING("jam", outcome_name(Outcome::Jam));
    TEST_ASSERT_EQUAL_STRING("error", outcome_name(Outcome::Error));
    TEST_ASSERT_EQUAL_STRING("door_open", outcome_name(Outcome::DoorOpen));
    TEST_ASSERT_EQUAL_STRING("no_flow", outcome_name(Outcome::NoFlow));
    TEST_ASSERT_EQUAL_STRING("over_dispense", outcome_name(Outcome::OverDispense));
}

static void test_catch_on_before_any_motor(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    TEST_ASSERT_TRUE(r.d.start(c, 0));
    r.tick();
    TEST_ASSERT_TRUE(r.out[kCatch]);
    TEST_ASSERT_FALSE(r.out[kAgit]);
    TEST_ASSERT_FALSE(r.out[kAuger]);
    r.run_to(3500);
    TEST_ASSERT_EQUAL_INT32(0, r.on_at[kCatch]);
    TEST_ASSERT_TRUE(r.on_at[kAgit] > r.on_at[kCatch]);
    TEST_ASSERT_TRUE(r.on_at[kAuger] > r.on_at[kCatch]);
}

static void test_fan_lead_and_lag(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(3000);
    r.set_bag_full(true);
    r.run_to(20000);
    TEST_ASSERT_EQUAL_INT32(0, r.on_at[kFan]);
    TEST_ASSERT_EQUAL_INT32(1000, r.on_at[kAgit]);
    TEST_ASSERT_EQUAL_INT32(r.on_at[kAgit] - 1000, r.on_at[kFan]);
    // fan stays on 2000 ms after fill ends (= auger off)
    TEST_ASSERT_EQUAL_INT32(r.off_at[kAuger] + 2000, r.off_at[kFan]);
}

static void test_agitate_runs_exactly_run_s(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(5000);
    TEST_ASSERT_EQUAL_INT32(1000, r.on_at[kAgit]);
    TEST_ASSERT_EQUAL_INT32(3000, r.off_at[kAgit]);
    TEST_ASSERT_EQUAL_INT32(3000, r.on_at[kAuger]);
}

static void test_fill_ends_on_sensor_complete(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(11190);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.set_bag_full(true);
    r.run_to(11200);  // asserted at 11200 (debounce 0); fill began at 3000
    TEST_ASSERT_EQUAL_INT32(11200, r.off_at[kAuger]);
    TEST_ASSERT_EQUAL_INT32(11200, r.off_at[kCatch]);  // ByCatch: catch off at release entry
    r.run_to(14190);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(14200);  // + drop_s 3 s
    TEST_ASSERT_EQUAL(1, r.outcomes());
    const Event* o = r.outcome();
    TEST_ASSERT_EQUAL_UINT8(3, o->slot);
    TEST_ASSERT_EQUAL_STRING("req-1", o->request_id);
    TEST_ASSERT_EQUAL_STRING_LEN("bag_full after 8.2 s", o->text, 20);
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)o->value);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_fill_max_run_bin_empty(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(22990);
    TEST_ASSERT_TRUE(r.out[kCatch]);
    TEST_ASSERT_TRUE(r.out[kAuger]);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(23000);  // fill began 3000 + max_run_s 20
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL((int)Outcome::BinEmpty, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("max_run_s reached", r.outcome()->text);
    TEST_ASSERT_EQUAL_INT32(23000, r.off_at[kCatch]);
    TEST_ASSERT_EQUAL_INT32(23000, r.off_at[kFan]);
    TEST_ASSERT_EQUAL((int)Dispenser::State::Done, (int)r.d.state());
    r.run_to(30000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

static void test_timed_fill_max_run_is_complete(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_timed);
    r.d.start(c, 0);
    r.run_to(20000);
    TEST_ASSERT_EQUAL_INT32(0, r.on_at[kAgit]);
    TEST_ASSERT_EQUAL_INT32(2000, r.off_at[kAgit]);
    TEST_ASSERT_EQUAL_INT32(2000, r.on_at[kAuger]);
    TEST_ASSERT_EQUAL_INT32(12000, r.off_at[kAuger]);
    // solenoid (trap_door_catch) pulses 1.5 s after the fill
    TEST_ASSERT_EQUAL_INT32(12000, r.on_at[kCatch]);
    TEST_ASSERT_EQUAL_INT32(13500, r.off_at[kCatch]);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("max_run_s reached", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_abort_mid_fill(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(5000);
    TEST_ASSERT_TRUE(r.out[kAuger]);
    r.d.abort(r.t, "operator stop", r.guard, r.pub);
    r.guard.apply(r.t, r.out, r.pub);
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL(1, r.outcomes());
    r.tick();
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Error, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("operator stop", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL((int)Dispenser::State::Done, (int)r.d.state());
    r.run_to(30000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

static void test_first_actuator_on(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    TEST_ASSERT_FALSE(r.d.first_actuator_on());
    r.d.start(c, 0);
    TEST_ASSERT_FALSE(r.d.first_actuator_on());
    r.tick();
    TEST_ASSERT_TRUE(r.out[kCatch]);
    TEST_ASSERT_TRUE(r.d.first_actuator_on());
}

static void test_start_while_running_or_done_refused_then_finish(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    TEST_ASSERT_TRUE(r.d.start(c, 0));
    TEST_ASSERT_FALSE(r.d.start(c, 0));
    r.run_to(23000);
    TEST_ASSERT_EQUAL((int)Dispenser::State::Done, (int)r.d.state());
    TEST_ASSERT_FALSE(r.d.start(c, r.t));
    r.d.finish();
    TEST_ASSERT_EQUAL((int)Dispenser::State::Idle, (int)r.d.state());
    TEST_ASSERT_TRUE(r.d.start(c, r.t));
    TEST_ASSERT_EQUAL_STRING("req-1", r.d.request_id());
    TEST_ASSERT_EQUAL_UINT8(3, r.d.slot());
}

static void test_step_events_in_order(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_catch);
    r.d.start(c, 0);
    r.run_to(3000);
    r.set_bag_full(true);
    r.run_to(10000);
    const char* want[3] = {"agitate", "fill", "release"};
    int n = 0;
    for (int i = 0; i < r.pub.count; ++i) {
        if (r.pub.events[i].type != EventType::Step) continue;
        TEST_ASSERT_TRUE(n < 3);
        TEST_ASSERT_EQUAL_STRING(want[n], r.pub.events[i].text);
        TEST_ASSERT_EQUAL_STRING("req-1", r.pub.events[i].request_id);
        TEST_ASSERT_EQUAL_UINT8(3, r.pub.events[i].slot);
        ++n;
    }
    TEST_ASSERT_EQUAL(3, n);
}

// Timed fill (agitate 2 s, fill 10 s) then a door_sensor release: pulse 1.5 s, open timeout 2 s,
// close timeout 3 s. Solenoid = trap_door_catch, door sensor = service_door.
static const char* const kDoorSensorProfile = R"JSON({
  "mechanism": "bagged_ice",
  "product_sku": "ice-bag",
  "agitate": {"motor_channel": "agitator_motor", "run_seconds": 2, "stall_current_amps": "unmonitored", "current_channel": "unmonitored"},
  "fill": {"proof": "timed", "motor_channel": "auger_motor", "max_run_seconds": 10, "stall_current_amps": "unmonitored", "current_channel": "unmonitored"},
  "release": {"proof": "door_sensor", "solenoid_channel": "trap_door_catch", "sensor_channel": "service_door", "pulse_seconds": 1.5, "open_timeout_seconds": 2, "close_timeout_seconds": 3}
})JSON";

static void set_door(Rig& r, bool v) { r.raw.binary[static_cast<int>(In::ServiceDoor)] = v; }

static void test_by_sensor_release_complete(void) {
    Rig r;
    Command c = make_cmd(kDoorSensorProfile);
    r.d.start(c, 0);
    r.run_to(13490);
    TEST_ASSERT_EQUAL_INT32(12000, r.on_at[kCatch]);
    TEST_ASSERT_TRUE(r.out[kCatch]);
    r.run_to(13500);
    TEST_ASSERT_EQUAL_INT32(13500, r.off_at[kCatch]);  // solenoid on for exactly pulse_s
    set_door(r, true);
    r.run_to(14000);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    set_door(r, false);
    r.run_to(14010);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.all_off());
    r.run_to(30000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

static void test_by_sensor_door_never_opens_is_error(void) {
    Rig r;
    Command c = make_cmd(kDoorSensorProfile);
    r.d.start(c, 0);
    r.run_to(15490);  // pulse ends 13500 + open_timeout 2 s
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(15500);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Error, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("door did not open", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
    r.run_to(30000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

static void test_by_sensor_door_never_closes_is_door_open(void) {
    Rig r;
    Command c = make_cmd(kDoorSensorProfile);
    r.d.start(c, 0);
    r.run_to(13990);
    set_door(r, true);  // seen open at 14000; close timeout 3 s
    r.run_to(16990);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(17000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::DoorOpen, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("door did not close", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
    r.run_to(30000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

static void test_no_agitate_goes_straight_to_fill(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_ice_timed);
    c.profile.agitate.motor = -1;  // the parser requires an agitate block; the engine skips it at -1
    r.d.start(c, 0);
    r.run_to(100);
    TEST_ASSERT_TRUE(r.out[kAuger]);
    TEST_ASSERT_FALSE(r.out[kAgit]);
    int steps = 0;
    for (int i = 0; i < r.pub.count; ++i) {
        if (r.pub.events[i].type != EventType::Step) continue;
        TEST_ASSERT_TRUE(strcmp(r.pub.events[i].text, "agitate") != 0);
        if (steps++ == 0) TEST_ASSERT_EQUAL_STRING("fill", r.pub.events[i].text);
    }
    TEST_ASSERT_EQUAL(1, steps);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_names);
    RUN_TEST(test_catch_on_before_any_motor);
    RUN_TEST(test_fan_lead_and_lag);
    RUN_TEST(test_agitate_runs_exactly_run_s);
    RUN_TEST(test_fill_ends_on_sensor_complete);
    RUN_TEST(test_fill_max_run_bin_empty);
    RUN_TEST(test_timed_fill_max_run_is_complete);
    RUN_TEST(test_abort_mid_fill);
    RUN_TEST(test_first_actuator_on);
    RUN_TEST(test_start_while_running_or_done_refused_then_finish);
    RUN_TEST(test_step_events_in_order);
    RUN_TEST(test_by_sensor_release_complete);
    RUN_TEST(test_by_sensor_door_never_opens_is_error);
    RUN_TEST(test_by_sensor_door_never_closes_is_door_open);
    RUN_TEST(test_no_agitate_goes_straight_to_fill);
    return UNITY_END();
}
