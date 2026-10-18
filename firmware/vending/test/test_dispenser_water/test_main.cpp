#include <string.h>
#include <unity.h>

#include <ArduinoJson.h>

#include "../fixtures/fixtures.h"
#include "../support/fakes.h"
#include "channels.h"
#include "command.h"
#include "dispenser.h"
#include "inputs.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

constexpr int kValve = static_cast<int>(Out::WaterValve);
constexpr int kFan = static_cast<int>(Out::BagFan);
constexpr uint8_t kFillPulses = 16;  // index of fill_pulses in kChannels

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
    bool out[kOutputCount] = {};
    uint32_t t = 0;
    uint32_t dt = 10;  // tick period in ms
    // First tick time each output was seen on / last seen turning off (-1 = never).
    int32_t on_at[kOutputCount];
    int32_t off_at[kOutputCount];
    bool prev[kOutputCount] = {};

    Rig() {
        for (int i = 0; i < kOutputCount; ++i) on_at[i] = off_at[i] = -1;
    }
    void tick() {
        sc.scan(raw, t, pub);
        d.tick(t, sc, out, pub);
        for (int i = 0; i < kOutputCount; ++i) {
            if (out[i] && !prev[i] && on_at[i] < 0) on_at[i] = (int32_t)t;
            if (!out[i] && prev[i]) off_at[i] = (int32_t)t;
            prev[i] = out[i];
        }
        t += dt;
    }
    // Tick every 10 ms through time `until` inclusive.
    void run_to(uint32_t until) {
        while (t <= until) tick();
    }
    uint32_t base = 0;     // counter value when the run starts
    uint32_t start_t = 0;  // time the run starts
    // Until time `until` inclusive, the tick n ms-steps after start sees base + min(per_tick * n, cap) pulses.
    void ramp(uint32_t until, uint32_t per_tick, uint32_t cap) {
        while (t <= until) {
            uint32_t want = per_tick * ((t - start_t) / 10);
            raw.pulses = base + (want > cap ? cap : want);
            tick();
        }
    }
    int telemetry() const {
        int n = 0;
        for (int i = 0; i < pub.count; ++i) n += pub.events[i].type == EventType::Telemetry;
        return n;
    }
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

static const char* kFanWater = R"JSON({
  "mechanism": "water_fill",
  "product_sku": "water-1gal",
  "fill": {"proof": "flow_volume", "valve_channel": "water_valve_solenoid", "flow_sensor_channel": "fill_pulses", "target_volume_ml": 3785, "pulses_per_liter": 450, "min_flow_ml_per_second": 20, "no_flow_grace_seconds": 3, "over_dispense_percent": 5, "max_fill_seconds": 120},
  "accessories": {"fan": {"channel": "bag_fan", "on_during": ["fill"], "lead_seconds": 1, "lag_seconds": 2}}
})JSON";

static Command make_cmd(const char* fixture) {
    static JsonDocument doc;
    doc.clear();
    deserializeJson(doc, fixture);
    Command c = {};
    c.kind = CmdKind::Dispense;
    strcpy(c.request_id, "req-w");
    c.slot = 5;
    c.mechanism = Mechanism::WaterFill;
    ParseResult r = parse_profile(doc.as<JsonVariantConst>(), Mechanism::WaterFill, c.profile);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    return c;
}

static void test_valve_opens_on_start(void) {
    Rig r;
    Command c = make_cmd(kFixture_profile_water_1gal);
    TEST_ASSERT_TRUE(r.d.start(c, 0));
    r.tick();
    TEST_ASSERT_TRUE(r.out[kValve]);
    TEST_ASSERT_TRUE(r.d.first_actuator_on());
    int steps = 0;
    for (int i = 0; i < r.pub.count; ++i) {
        if (r.pub.events[i].type != EventType::Step) continue;
        TEST_ASSERT_EQUAL_STRING("fill", r.pub.events[i].text);
        ++steps;
    }
    TEST_ASSERT_EQUAL(1, steps);
}

static void test_target_closes_valve_then_settles_complete(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(5000, 5, 1703);  // 500 pulses/s; 1703 reached at t = 3410 (tick 341)
    TEST_ASSERT_EQUAL_INT32(3410, r.off_at[kValve]);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("1703 pulses", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_settle_waits_one_second_after_last_pulse(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(3400, 5, 1703);
    r.raw.pulses = 1703;
    r.tick();  // t = 3410: target reached, valve off
    TEST_ASSERT_EQUAL_INT32(3410, r.off_at[kValve]);
    r.run_to(4400);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.tick();  // t = 4410: 1000 ms of quiet
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL_STRING("1703 pulses", r.outcome()->text);
}

static void test_late_pulse_in_settle_extends_wait_and_counts(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(3400, 5, 1703);
    r.raw.pulses = 1703;
    r.tick();  // t = 3410
    r.run_to(3900);
    r.raw.pulses = 1710;  // within the 5 % margin (limit 1788)
    r.tick();             // t = 3910
    r.run_to(4900);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.tick();  // t = 4910
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL_STRING("1710 pulses", r.outcome()->text);
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
}

static void test_no_flow_after_grace_plus_one_tick(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.run_to(3000);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    TEST_ASSERT_TRUE(r.out[kValve]);
    r.tick();  // t = 3010 > grace
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::NoFlow, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_flow_stopping_trips_no_flow_later(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(2990, 1, 100000);  // 100 pulses/s = 222 ml/s
    r.run_to(5000);           // flow stops at 3000; the window drains by ~4000
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(9000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::NoFlow, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_over_dispense_when_pulses_continue_after_valve_off(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(5000, 5, 1800);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::OverDispense, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.off_at[kValve] >= 0);
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL_STRING("1790 pulses, target 1703", r.outcome()->text);
}

static void test_timeout_when_stuck_below_target(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    // one pulse every 80 ms = 12.5/s = 27.8 ml/s, above the 20 ml/s floor; 1500 pulses in 120 s
    for (uint32_t i = 0; r.t <= 119990; ++i) {
        if (i % 8 == 0) r.raw.pulses++;
        r.tick();
    }
    TEST_ASSERT_EQUAL(0, r.outcomes());
    TEST_ASSERT_TRUE(r.out[kValve]);
    r.tick();  // t = 120000
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Timeout, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("max_fill_s reached", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_telemetry_every_second_during_fill_none_after(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(3500, 1, 100000);  // 100 pulses/s, short of target in this span
    TEST_ASSERT_EQUAL(3, r.telemetry());
    int seen = 0;
    for (int i = 0; i < r.pub.count; ++i) {
        const Event& e = r.pub.events[i];
        if (e.type != EventType::Telemetry) continue;
        TEST_ASSERT_EQUAL(kFillPulses, e.channel);
        TEST_ASSERT_EQUAL_STRING("req-w", e.request_id);
        ++seen;
        // the tick at t = 10 n has n pulses; telemetry fires at t = 1000 * seen
        TEST_ASSERT_EQUAL_UINT32(100u * seen, e.value);
    }
    TEST_ASSERT_EQUAL(3, seen);
    // finish the fill; no more telemetry once the valve is off
    r.ramp(20000, 20, 1703);
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    const int at_off = r.telemetry();
    r.run_to(25000);
    TEST_ASSERT_EQUAL(at_off, r.telemetry());
    TEST_ASSERT_TRUE(at_off <= 5);
}

static void test_counter_wrap(void) {
    Rig r;
    r.raw.pulses = 0xFFFFFF00u;
    r.base = 0xFFFFFF00u;
    r.tick();  // scanner sees the starting count before the run begins
    r.start_t = r.t;
    r.d.start(make_cmd(kFixture_profile_water_1gal), r.t);
    r.ramp(9000, 10, 1703);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("1703 pulses", r.outcome()->text);
}

static void test_accessory_follows_valve_with_lead_and_lag(void) {
    Rig r;
    r.d.start(make_cmd(kFanWater), 0);
    r.run_to(1000);
    TEST_ASSERT_EQUAL_INT32(0, r.on_at[kFan]);
    TEST_ASSERT_EQUAL_INT32(1000, r.on_at[kValve]);
    r.ramp(9000, 20, 1703);
    TEST_ASSERT_TRUE(r.off_at[kValve] > 1000);
    TEST_ASSERT_EQUAL_INT32(r.off_at[kValve] + 2000, r.off_at[kFan]);
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_timed_water_runs_max_fill_then_complete(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_timed), 0);
    r.run_to(29990);
    TEST_ASSERT_TRUE(r.out[kValve]);
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.tick();  // t = 30000
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("max_fill_s reached", r.outcome()->text);
    TEST_ASSERT_EQUAL_INT32(30000, r.off_at[kValve]);
    TEST_ASSERT_EQUAL(0, r.telemetry());
}

static void test_abort_mid_fill(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.run_to(500);
    r.d.abort(r.t, "abort", r.pub);
    r.tick();
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Error, (int)r.outcome()->value);
}

static void test_no_flow_trips_with_5ms_ticks(void) {
    Rig r;
    r.dt = 5;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    while (r.t < 3000) {
        r.raw.pulses = r.t / 10;  // 100 pulses/s = 222 ml/s
        r.tick();
    }
    r.run_to(5000);  // flow stopped at 3000; the window drains by ~4000
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.run_to(7200);  // below the floor from ~4000, grace 3 s
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::NoFlow, (int)r.outcome()->value);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_complete_with_20ms_ticks(void) {
    Rig r;
    r.dt = 20;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(6000, 5, 1703);  // 250 pulses/s per 20 ms tick pair; cap 1703
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("1703 pulses", r.outcome()->text);
    TEST_ASSERT_TRUE(r.all_off());
}

static void test_settle_capped_at_5s_under_trickle(void) {
    Rig r;
    r.d.start(make_cmd(kFixture_profile_water_1gal), 0);
    r.ramp(3400, 5, 1703);
    r.raw.pulses = 1703;
    r.tick();  // t = 3410: valve off
    TEST_ASSERT_EQUAL_INT32(3410, r.off_at[kValve]);
    while (r.t < 3410 + 5000) {
        // one pulse every 900 ms after valve-off
        r.raw.pulses = 1703 + (r.t - 3410) / 900;
        r.tick();
    }
    TEST_ASSERT_EQUAL(0, r.outcomes());
    r.raw.pulses = 1703 + (r.t - 3410) / 900;
    r.tick();  // t = 8410
    TEST_ASSERT_EQUAL(1, r.outcomes());
    TEST_ASSERT_EQUAL((int)Outcome::Complete, (int)r.outcome()->value);
    TEST_ASSERT_EQUAL_STRING("1708 pulses", r.outcome()->text);
    r.run_to(12000);
    TEST_ASSERT_EQUAL(1, r.outcomes());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_valve_opens_on_start);
    RUN_TEST(test_target_closes_valve_then_settles_complete);
    RUN_TEST(test_settle_waits_one_second_after_last_pulse);
    RUN_TEST(test_late_pulse_in_settle_extends_wait_and_counts);
    RUN_TEST(test_no_flow_after_grace_plus_one_tick);
    RUN_TEST(test_flow_stopping_trips_no_flow_later);
    RUN_TEST(test_over_dispense_when_pulses_continue_after_valve_off);
    RUN_TEST(test_timeout_when_stuck_below_target);
    RUN_TEST(test_telemetry_every_second_during_fill_none_after);
    RUN_TEST(test_counter_wrap);
    RUN_TEST(test_accessory_follows_valve_with_lead_and_lag);
    RUN_TEST(test_timed_water_runs_max_fill_then_complete);
    RUN_TEST(test_abort_mid_fill);
    RUN_TEST(test_no_flow_trips_with_5ms_ticks);
    RUN_TEST(test_complete_with_20ms_ticks);
    RUN_TEST(test_settle_capped_at_5s_under_trickle);
    return UNITY_END();
}
