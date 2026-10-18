#include <string.h>
#include <unity.h>

#include <ArduinoJson.h>

#include "../fixtures/fixtures.h"
#include "../support/fakes.h"
#include "core.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

constexpr int kCatch = static_cast<int>(Out::TrapDoorCatch);
constexpr int kValve = static_cast<int>(Out::WaterValve);

static CoreConfig base_cfg() {
    CoreConfig c;
    for (int i = 0; i < kBinaryInputCount; ++i) c.polarity.active_low[i] = false;
    return c;
}

static Command make_cmd(const char* fixture, Mechanism m, const char* id) {
    static JsonDocument doc;
    doc.clear();
    deserializeJson(doc, fixture);
    Command c = {};
    c.kind = CmdKind::Dispense;
    strcpy(c.name, "dispense");
    strcpy(c.request_id, id);
    c.slot = 3;
    c.mechanism = m;
    ParseResult r = parse_profile(doc.as<JsonVariantConst>(), m, c.profile);
    TEST_ASSERT_TRUE_MESSAGE(r.ok, r.detail);
    return c;
}

static Decoded dispense(const char* id) {
    Decoded d = {};
    d.result = Decode::Ok;
    d.cmd = make_cmd(kFixture_profile_ice_catch, Mechanism::BaggedIce, id);
    return d;
}

static Decoded simple(CmdKind k, const char* name, const char* id) {
    Decoded d = {};
    d.result = Decode::Ok;
    d.cmd.kind = k;
    strcpy(d.cmd.name, name);
    strcpy(d.cmd.request_id, id);
    return d;
}

static Decoded valve(int seconds, const char* id) {
    Decoded d = simple(CmdKind::WaterValve, "water_valve", id);
    d.cmd.valve_seconds = static_cast<uint8_t>(seconds);
    return d;
}

struct Rig {
    FakeClock clk;
    FakeIo io;
    VecPublisher pub;
    Core core;
    int ticks = 0;

    explicit Rig(const CoreConfig& cfg = base_cfg()) : core(cfg, clk, io, pub) {
        io.current.master_enable = true;
        io.current.ambient_valid = true;
        io.current.ambient_c = 20.0f;
        io.current.binary[static_cast<int>(In::RoTankFull)] = true;  // keep the RO pump quiet
    }
    void tick() {
        core.tick();
        clk.t += 10;
        ++ticks;
    }
    // Tick every 10 ms until the clock passes `until`.
    void run_to(uint32_t until) {
        while (clk.t <= until) tick();
    }
    void set_in(In i, bool v) { io.current.binary[static_cast<int>(i)] = v; }
    int count(EventType t) const {
        int n = 0;
        for (int i = 0; i < pub.count; ++i) n += pub.events[i].type == t;
        return n;
    }
    const Event* find(EventType t) const {
        for (int i = 0; i < pub.count; ++i)
            if (pub.events[i].type == t) return &pub.events[i];
        return nullptr;
    }
    bool all_off() const {
        for (int i = 0; i < kOutputCount; ++i)
            if (io.last_outputs[i]) return false;
        return true;
    }
};

static void test_accepted_after_catch_written(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    Ack a = r.core.submit(dispense("req-0001"));
    TEST_ASSERT_TRUE(a.status == AckStatus::Ok);
    TEST_ASSERT_TRUE(a.phase == Phase::Accepted);
    TEST_ASSERT_EQUAL_STRING("req-0001", a.request_id);
    TEST_ASSERT_EQUAL_STRING("dispense", a.command);
    TEST_ASSERT_TRUE(r.io.last_outputs[kCatch]);
}

static void test_full_ice_run(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    Ack a = r.core.submit(dispense("req-0001"));
    TEST_ASSERT_TRUE(a.phase == Phase::Accepted);
    r.run_to(3000);
    r.set_in(In::BagFull, true);
    while (r.clk.t < 30000 && r.count(EventType::Outcome) == 0) {
        r.core.vmc_seen();
        r.tick();
    }
    TEST_ASSERT_EQUAL(3, r.count(EventType::Step));
    TEST_ASSERT_EQUAL(1, r.count(EventType::Outcome));
    const Event* o = r.find(EventType::Outcome);
    TEST_ASSERT_EQUAL_UINT32(static_cast<uint32_t>(Outcome::Complete), o->value);
    TEST_ASSERT_EQUAL_STRING("req-0001", o->request_id);
    r.tick();
    // The next command is accepted: the Done run was finished.
    r.core.vmc_seen();
    Ack b = r.core.submit(dispense("req-0002"));
    TEST_ASSERT_TRUE(b.phase == Phase::Accepted);
}

static void test_second_dispense_busy(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    r.core.submit(dispense("req-0001"));
    r.run_to(500);
    Ack b = r.core.submit(dispense("req-0002"));
    TEST_ASSERT_TRUE(b.status == AckStatus::Rejected);
    TEST_ASSERT_EQUAL_STRING("busy", b.detail);
    Ack v = r.core.submit(valve(2, "req-0003"));
    TEST_ASSERT_EQUAL_STRING("busy", v.detail);
}

static void test_vmc_offline(void) {
    CoreConfig cfg = base_cfg();
    cfg.silence_ms = 2000;
    Rig r(cfg);
    r.tick();
    Ack a = r.core.submit(dispense("req-0001"));
    TEST_ASSERT_TRUE(a.status == AckStatus::Rejected);
    TEST_ASSERT_EQUAL_STRING("vmc_offline", a.detail);
    Ack v = r.core.submit(valve(2, "req-0002"));
    TEST_ASSERT_EQUAL_STRING("vmc_offline", v.detail);

    r.core.vmc_seen();
    TEST_ASSERT_TRUE(r.core.submit(dispense("req-0003")).phase == Phase::Accepted);
    r.run_to(1500);
    TEST_ASSERT_EQUAL(0, r.count(EventType::Outcome));
    r.run_to(3500);
    const Event* o = r.find(EventType::Outcome);
    TEST_ASSERT_NOT_NULL(o);
    TEST_ASSERT_EQUAL_UINT32(static_cast<uint32_t>(Outcome::Error), o->value);
    TEST_ASSERT_EQUAL_STRING("vmc_offline", o->text);
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_EQUAL(1, r.count(EventType::Outcome));
}

static void test_silence_disabled(void) {
    Rig r;  // default silence_ms == 0
    r.tick();
    r.core.vmc_online(true);
    r.run_to(600000);
    TEST_ASSERT_TRUE(r.core.submit(dispense("req-0001")).phase == Phase::Accepted);
    // Let the first run finish: "busy" is checked before "vmc_offline", so a still-busy
    // dispenser would reject the second submit for the wrong reason.
    r.set_in(In::BagFull, true);
    const uint32_t deadline = r.clk.t + 60000;
    while (r.clk.t < deadline && r.count(EventType::Outcome) == 0) r.tick();
    TEST_ASSERT_EQUAL(1, r.count(EventType::Outcome));
    r.tick();
    r.core.vmc_online(false);
    Ack a = r.core.submit(dispense("req-0002"));
    TEST_ASSERT_TRUE(a.status == AckStatus::Rejected);
    TEST_ASSERT_EQUAL_STRING("vmc_offline", a.detail);
}

static void test_water_valve_test(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    Ack a = r.core.submit(valve(3, "req-0001"));
    TEST_ASSERT_TRUE(a.status == AckStatus::Ok);
    TEST_ASSERT_TRUE(a.phase == Phase::Accepted);
    TEST_ASSERT_EQUAL_STRING("water_valve", a.command);
    TEST_ASSERT_TRUE(r.io.last_outputs[kValve]);
    Ack c;
    r.run_to(2900);
    r.core.vmc_seen();
    TEST_ASSERT_FALSE(r.core.poll_completion(c));
    TEST_ASSERT_TRUE(r.io.last_outputs[kValve]);
    r.run_to(3100);
    TEST_ASSERT_TRUE(r.core.poll_completion(c));
    TEST_ASSERT_TRUE(c.status == AckStatus::Ok);
    TEST_ASSERT_TRUE(c.phase == Phase::Completed);
    TEST_ASSERT_EQUAL_STRING("req-0001", c.request_id);
    TEST_ASSERT_EQUAL_STRING("water_valve", c.command);
    TEST_ASSERT_FALSE(r.core.poll_completion(c));
    TEST_ASSERT_FALSE(r.io.last_outputs[kValve]);
    // valve tests emit OutputChanged only
    TEST_ASSERT_EQUAL(0, r.count(EventType::Step));
    TEST_ASSERT_EQUAL(0, r.count(EventType::Outcome));
}

static void test_sump_high_mid_run(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    r.core.submit(dispense("req-0001"));
    r.run_to(1000);
    r.set_in(In::SumpHigh, true);
    r.run_to(1200);
    const Event* o = r.find(EventType::Outcome);
    TEST_ASSERT_NOT_NULL(o);
    TEST_ASSERT_EQUAL_UINT32(static_cast<uint32_t>(Outcome::Error), o->value);
    TEST_ASSERT_EQUAL_STRING("sump_high", o->text);
    TEST_ASSERT_TRUE(r.all_off());
    TEST_ASSERT_FALSE(r.core.health().ok());  // latched
}

static void test_sump_high_valve_test_fails_the_completion(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    TEST_ASSERT_TRUE(r.core.submit(valve(5, "req-0001")).phase == Phase::Accepted);
    r.run_to(500);
    r.set_in(In::SumpHigh, true);
    r.run_to(700);
    Ack c;
    TEST_ASSERT_TRUE(r.core.poll_completion(c));
    TEST_ASSERT_TRUE(c.status == AckStatus::Failed);
    TEST_ASSERT_EQUAL_STRING("sump_high", c.detail);
    TEST_ASSERT_FALSE(r.io.last_outputs[kValve]);
}

static void test_io_failure_withholds_kick(void) {
    Rig r;
    r.tick();
    r.tick();
    TEST_ASSERT_EQUAL(2, r.io.kicks);
    r.io.read_ok = false;
    r.tick();
    TEST_ASSERT_EQUAL(2, r.io.kicks);
    TEST_ASSERT_FALSE(r.core.health().ok());
    TEST_ASSERT_TRUE(r.all_off());
    r.io.read_ok = true;
    r.tick();
    TEST_ASSERT_EQUAL(3, r.io.kicks);
    TEST_ASSERT_TRUE(r.core.health().ok());
}

static void test_kick_count_equals_healthy_ticks(void) {
    Rig r;
    for (int i = 0; i < 20; ++i) r.tick();
    r.io.read_ok = false;
    r.tick();
    r.tick();
    r.io.read_ok = true;
    for (int i = 0; i < 20; ++i) r.tick();
    TEST_ASSERT_EQUAL(42, r.ticks);
    TEST_ASSERT_EQUAL(40, r.io.kicks);
}

static void test_missed_deadline_withholds_kick(void) {
    Rig r;
    r.tick();
    r.tick();
    r.clk.t += 100;  // a stalled tick
    r.tick();
    TEST_ASSERT_EQUAL(2, r.io.kicks);
    r.tick();
    TEST_ASSERT_EQUAL(3, r.io.kicks);
}

static void test_force_report(void) {
    Rig r;
    for (int i = 0; i < 5; ++i) r.tick();
    r.pub.clear();
    r.core.submit(simple(CmdKind::ForceReport, "force_report", "req-0001"));
    r.tick();
    TEST_ASSERT_EQUAL(kBinaryInputCount, r.count(EventType::InputChanged));
    TEST_ASSERT_EQUAL(kOutputCount, r.count(EventType::OutputChanged));
    TEST_ASSERT_EQUAL(1, r.count(EventType::Temperature));
    const Event* t = r.find(EventType::Temperature);
    TEST_ASSERT_EQUAL_UINT8(channel_index("ambient"), t->channel);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, t->fvalue);
    r.pub.clear();
    r.tick();
    TEST_ASSERT_EQUAL(0, r.pub.count);
}

static void test_periodic_republish_and_temperature(void) {
    CoreConfig cfg = base_cfg();
    cfg.io_republish_ms = 1000;
    cfg.temp_publish_ms = 500;
    Rig r(cfg);
    r.tick();  // t=0: first temperature
    TEST_ASSERT_EQUAL(1, r.count(EventType::Temperature));
    r.pub.clear();
    r.run_to(990);
    TEST_ASSERT_EQUAL(0, r.count(EventType::InputChanged));
    TEST_ASSERT_EQUAL(1, r.count(EventType::Temperature));  // t=500
    r.run_to(1000);
    TEST_ASSERT_EQUAL(kBinaryInputCount, r.count(EventType::InputChanged));
    r.run_to(1990);
    TEST_ASSERT_EQUAL(kBinaryInputCount, r.count(EventType::InputChanged));
    r.run_to(2000);
    TEST_ASSERT_EQUAL(2 * kBinaryInputCount, r.count(EventType::InputChanged));
}

static void test_self_test_lists_six_checks(void) {
    Rig r;
    r.core.vmc_seen();
    r.tick();
    Ack a = r.core.submit(simple(CmdKind::SelfTest, "self_test", "req-0001"));
    TEST_ASSERT_TRUE(a.status == AckStatus::Ok);
    TEST_ASSERT_TRUE(a.phase == Phase::Completed);
    JsonDocument doc;
    TEST_ASSERT_TRUE(deserializeJson(doc, a.result_json) == DeserializationError::Ok);
    JsonArray checks = doc["checks"];
    TEST_ASSERT_EQUAL(6, (int)checks.size());
    const char* names[6] = {"i2c_expanders", "ambient_sensor", "watchdog_kick",
                            "vmc_seen",      "master_enable",  "dropped_events"};
    for (int i = 0; i < 6; ++i) {
        TEST_ASSERT_EQUAL_STRING(names[i], checks[i]["name"].as<const char*>());
        TEST_ASSERT_TRUE(checks[i]["pass"].as<bool>());
    }
    Ack direct;
    r.core.self_test(direct);
    TEST_ASSERT_EQUAL_STRING(a.result_json, direct.result_json);

    r.io.read_ok = false;
    r.tick();
    Ack f = r.core.submit(simple(CmdKind::SelfTest, "self_test", "req-0002"));
    deserializeJson(doc, f.result_json);
    TEST_ASSERT_FALSE(doc["checks"][0]["pass"].as<bool>());
    TEST_ASSERT_FALSE(doc["checks"][2]["pass"].as<bool>());
}

static void test_ping_unsupported_rejected(void) {
    Rig r;
    Ack p = r.core.submit(simple(CmdKind::Ping, "ping", "req-0001"));
    TEST_ASSERT_TRUE(p.status == AckStatus::Ok && p.phase == Phase::Completed);
    Decoded u = {};
    u.result = Decode::Unsupported;
    strcpy(u.cmd.request_id, "req-0002");
    strcpy(u.cmd.name, "frobnicate");
    Ack ua = r.core.submit(u);
    TEST_ASSERT_TRUE(ua.status == AckStatus::Unsupported);
    TEST_ASSERT_EQUAL_STRING("req-0002", ua.request_id);
    Decoded rj = {};
    rj.result = Decode::Rejected;
    strcpy(rj.cmd.request_id, "req-0003");
    strcpy(rj.detail, "seconds out of range [1, 10]");
    Ack ra = r.core.submit(rj);
    TEST_ASSERT_TRUE(ra.status == AckStatus::Rejected);
    TEST_ASSERT_EQUAL_STRING("seconds out of range [1, 10]", ra.detail);
}

static void test_dropped_events_counted(void) {
    Rig r;
    for (int i = 0; i < 400 && r.pub.count < VecPublisher::kCap; ++i) {
        r.core.force_report();
        r.tick();
    }
    r.core.force_report();
    r.tick();
    TEST_ASSERT_TRUE(r.core.dropped_events() > 0);
    Ack st = r.core.submit(simple(CmdKind::SelfTest, "self_test", "req-0001"));
    JsonDocument doc;
    deserializeJson(doc, st.result_json);
    TEST_ASSERT_FALSE(doc["checks"][5]["pass"].as<bool>());
}

static void test_ro_loop_and_heater_run_autonomously(void) {
    Rig r;
    r.set_in(In::RoTankFull, false);
    r.io.current.ambient_c = 1.0f;
    r.tick();
    r.tick();
    TEST_ASSERT_TRUE(r.io.last_outputs[static_cast<int>(Out::RoProduction)]);
    TEST_ASSERT_TRUE(r.io.last_outputs[static_cast<int>(Out::Heater)]);
    r.set_in(In::SumpHigh, true);  // sump stops the pump; the heater coil rides the master relay, so it goes too
    r.run_to(200);
    TEST_ASSERT_FALSE(r.io.last_outputs[static_cast<int>(Out::RoProduction)]);
    TEST_ASSERT_FALSE(r.io.last_outputs[static_cast<int>(Out::Heater)]);
    r.set_in(In::SumpHigh, false);
    r.run_to(400);
    TEST_ASSERT_TRUE(r.io.last_outputs[static_cast<int>(Out::Heater)]);
}

static int count_output_on(const Rig& r, int channel_out) {
    int n = 0;
    for (int i = 0; i < r.pub.count; ++i) {
        const Event& e = r.pub.events[i];
        if (e.type == EventType::OutputChanged && e.channel == kBinaryInputCount + channel_out && e.state) ++n;
    }
    return n;
}

// A silent VMC trips vmc_offline without latching, so the autonomous heater keeps running; after 24 h
// of continuous cold it rests exactly 5 min and comes back (the guard's forced latch never fires).
static int health_events(const Rig& r, const char* text) {
    int n = 0;
    for (int i = 0; i < r.pub.count; ++i)
        if (r.pub.events[i].type == EventType::Health && strcmp(r.pub.events[i].text, text) == 0) ++n;
    return n;
}

static void test_heater_duty_cycles_through_sustained_vmc_offline(void) {
    Rig r;  // VMC never seen: vmc_offline holds for the whole run
    r.io.current.ambient_c = 1.0f;
    int off_ticks = 0;
    bool seen_on = false;
    for (uint32_t t = 1000; t <= 87000000; t += 1000) {
        r.clk.t = t;
        r.core.tick();
        const bool on = r.io.last_outputs[static_cast<int>(Out::Heater)];
        if (seen_on && !on) ++off_ticks;
        if (on) seen_on = true;
        if (t == 43200000) TEST_ASSERT_TRUE(on);
    }
    TEST_ASSERT_EQUAL(300, off_ticks);
    TEST_ASSERT_TRUE(r.io.last_outputs[static_cast<int>(Out::Heater)]);
}

// Cold ambient + sump_high: the trip used to alternate sump_high <-> master_enable_dropped (the heater
// request dropped the master relay), kicking the watchdog every other tick. Now the heater is held off
// while the latching condition holds, so Health stays not-ok and the master relay is never exercised.
static void test_sump_high_with_cold_ambient_holds_heater_off_and_unhealthy(void) {
    Rig r;
    r.core.vmc_seen();
    r.io.current.ambient_c = 1.0f;
    r.set_in(In::SumpHigh, true);
    r.tick();
    TEST_ASSERT_FALSE(r.core.health().ok());
    const int kicks = r.io.kicks;
    r.io.current.master_enable = false;  // what the board does if the heater were ever energized
    for (int i = 0; i < 50; ++i) {
        r.core.vmc_seen();
        r.tick();
        TEST_ASSERT_FALSE(r.core.health().ok());
    }
    TEST_ASSERT_EQUAL(kicks, r.io.kicks);
    TEST_ASSERT_EQUAL(0, count_output_on(r, static_cast<int>(Out::Heater)));
    TEST_ASSERT_EQUAL(1, r.count(EventType::Health));
    TEST_ASSERT_EQUAL(1, health_events(r, "sump_high"));
}

static void test_service_door_refuses_dispense_without_energizing(void) {
    Rig r;
    r.core.vmc_seen();
    r.set_in(In::ServiceDoor, true);
    r.tick();
    Ack a = r.core.submit(dispense("req-0001"));
    TEST_ASSERT_TRUE(a.phase != Phase::Accepted);
    TEST_ASSERT_TRUE(a.status == AckStatus::Failed);
    TEST_ASSERT_EQUAL_STRING("service_door", a.detail);
    TEST_ASSERT_EQUAL(0, count_output_on(r, kCatch));
    TEST_ASSERT_FALSE(r.io.last_outputs[kCatch]);
    TEST_ASSERT_EQUAL(1, r.count(EventType::Outcome));
    TEST_ASSERT_EQUAL_STRING("service_door", r.find(EventType::Outcome)->text);
}

static void test_master_enable_dropped_refuses_dispense(void) {
    Rig r;
    r.core.vmc_seen();
    r.io.current.master_enable = false;
    r.tick();
    Ack a = r.core.submit(dispense("req-0001"));
    TEST_ASSERT_TRUE(a.phase != Phase::Accepted);
    TEST_ASSERT_TRUE(a.status == AckStatus::Failed);
    TEST_ASSERT_TRUE(r.all_off());
    for (int ch = 0; ch < kOutputCount; ++ch) TEST_ASSERT_EQUAL(0, count_output_on(r, ch));
}


static void test_sustained_master_enable_drop_emits_one_health_event(void) {
    Rig r;
    r.set_in(In::RoTankFull, false);  // the pump wants to run
    r.core.vmc_seen();
    r.io.current.master_enable = false;
    for (int i = 0; i < 50; ++i) r.tick();
    TEST_ASSERT_EQUAL(1, health_events(r, "master_enable_dropped"));
    TEST_ASSERT_EQUAL(1, r.count(EventType::Health));
    TEST_ASSERT_EQUAL(0, count_output_on(r, static_cast<int>(Out::RoProduction)));
}

static void test_master_enable_drop_with_vmc_silent_emits_one_event_per_detail(void) {
    Rig r;
    r.set_in(In::RoTankFull, false);
    r.io.current.master_enable = false;  // VMC never seen: also silent
    for (int i = 0; i < 50; ++i) r.tick();
    TEST_ASSERT_TRUE(r.count(EventType::Health) <= 2);
    TEST_ASSERT_TRUE(r.count(EventType::Health) >= 1);
}

static void test_trip_clears_then_second_onset_emits_again(void) {
    Rig r;
    r.set_in(In::RoTankFull, false);
    r.core.vmc_seen();
    r.io.current.master_enable = false;
    for (int i = 0; i < 20; ++i) r.tick();
    TEST_ASSERT_EQUAL(1, health_events(r, "master_enable_dropped"));
    r.io.current.master_enable = true;
    for (int i = 0; i < 20; ++i) r.tick();
    TEST_ASSERT_EQUAL(1, health_events(r, "master_enable_dropped"));
    r.io.current.master_enable = false;
    for (int i = 0; i < 20; ++i) r.tick();
    TEST_ASSERT_EQUAL(2, health_events(r, "master_enable_dropped"));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_accepted_after_catch_written);
    RUN_TEST(test_full_ice_run);
    RUN_TEST(test_second_dispense_busy);
    RUN_TEST(test_vmc_offline);
    RUN_TEST(test_silence_disabled);
    RUN_TEST(test_water_valve_test);
    RUN_TEST(test_sump_high_mid_run);
    RUN_TEST(test_sump_high_valve_test_fails_the_completion);
    RUN_TEST(test_io_failure_withholds_kick);
    RUN_TEST(test_kick_count_equals_healthy_ticks);
    RUN_TEST(test_missed_deadline_withholds_kick);
    RUN_TEST(test_force_report);
    RUN_TEST(test_periodic_republish_and_temperature);
    RUN_TEST(test_self_test_lists_six_checks);
    RUN_TEST(test_ping_unsupported_rejected);
    RUN_TEST(test_dropped_events_counted);
    RUN_TEST(test_ro_loop_and_heater_run_autonomously);
    RUN_TEST(test_heater_duty_cycles_through_sustained_vmc_offline);
    RUN_TEST(test_sump_high_with_cold_ambient_holds_heater_off_and_unhealthy);
    RUN_TEST(test_service_door_refuses_dispense_without_energizing);
    RUN_TEST(test_master_enable_dropped_refuses_dispense);
    RUN_TEST(test_sustained_master_enable_drop_emits_one_health_event);
    RUN_TEST(test_master_enable_drop_with_vmc_silent_emits_one_event_per_detail);
    RUN_TEST(test_trip_clears_then_second_onset_emits_again);
    return UNITY_END();
}
