#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "ack_cache.h"
#include "channels.h"
#include "dispenser.h"
#include "wire.h"

using namespace vendcore;

void setUp(void) {}
void tearDown(void) {}

static WireCtx ctx() {
    WireCtx c;
    c.machine_id = "vm-01";
    c.hardware_id = "esp32-a0b1c2d3e4f5";
    c.ip = "192.168.1.50";
    c.firmware = "0.1.0";
    return c;
}

static void golden(const char* name, const char* body) {
    char path[96];
    snprintf(path, sizeof(path), "test/golden/%s.json", name);
    FILE* f = fopen(path, "wb");
    TEST_ASSERT_NOT_NULL_MESSAGE(f, path);
    fputs(body, f);
    fputc('\n', f);
    fclose(f);
}

static Event ev(EventType t, const char* ch) {
    Event e;
    memset(&e, 0, sizeof(e));
    e.type = t;
    e.channel = ch != nullptr ? static_cast<uint8_t>(channel_index(ch)) : 0;
    return e;
}

static void expect_topic(const Event& e, const char* want) {
    char t[128];
    const size_t n = topic_for(e, ctx(), t, sizeof(t));
    TEST_ASSERT_EQUAL_UINT(strlen(want), n);
    TEST_ASSERT_EQUAL_STRING(want, t);
}

static void test_topics(void) {
    expect_topic(ev(EventType::InputChanged, "sump_high"), "vmc/vm-01/hardware/io/sump_high");
    expect_topic(ev(EventType::OutputChanged, "auger_motor"), "vmc/vm-01/hardware/io/auger_motor");
    expect_topic(ev(EventType::ButtonPressed, "button_ice"), "vmc/vm-01/hardware/buttons");
    expect_topic(ev(EventType::Step, nullptr), "vmc/vm-01/hardware/dispenser");
    expect_topic(ev(EventType::Outcome, nullptr), "vmc/vm-01/hardware/dispenser");
    expect_topic(ev(EventType::Telemetry, "fill_pulses"), "vmc/vm-01/telemetry/vending/fill_pulses");
    expect_topic(ev(EventType::Temperature, "ambient"), "vmc/vm-01/sensors/temp/ambient");
    char t[128];
    TEST_ASSERT_EQUAL_UINT(0, topic_for(ev(EventType::Health, nullptr), ctx(), t, sizeof(t)));
    char tiny[8];
    TEST_ASSERT_EQUAL_UINT(0, topic_for(ev(EventType::ButtonPressed, "button_ice"), ctx(), tiny, sizeof(tiny)));
}

static void test_capabilities(void) {
    static char buf[6144];
    const size_t n = capabilities_json(ctx(), buf, sizeof(buf));
    TEST_ASSERT_GREATER_THAN_UINT(0, n);
    TEST_ASSERT_EQUAL_UINT(strlen(buf), n);
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"subsystem\":\"vending\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"contract_version\":\"2.0.0\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"commands\":[\"ping\",\"self_test\",\"force_report\",\"dispense\",\"water_valve\"]"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"hardware_id\":\"esp32-a0b1c2d3e4f5\""));
    // Channels appear in table order, once each.
    const char* at = buf;
    for (int i = 0; i < kChannelCount; ++i) {
        char needle[96];
        snprintf(needle, sizeof(needle), "\"channel_id\":\"%s\"", kChannels[i].id);
        const char* p = strstr(at, needle);
        TEST_ASSERT_NOT_NULL_MESSAGE(p, kChannels[i].id);
        at = p + 1;
    }
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"direction\":\"output\",\"driven_by\":\"dispense\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"direction\":\"output\",\"driven_by\":\"water_valve\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"kind\":\"counter\",\"unit\":\"pulses\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"kind\":\"temperature\",\"unit\":\"C\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"interval_seconds\":10.00"));
    TEST_ASSERT_NULL(strstr(buf, "timestamp"));
    char small[64];
    TEST_ASSERT_EQUAL_UINT(0, capabilities_json(ctx(), small, sizeof(small)));
    golden("capabilities", buf);
}

static void test_heartbeat_and_lwt(void) {
    char b[96];
    TEST_ASSERT_EQUAL_UINT(strlen("{\"subsystem\":\"vending\",\"uptime_seconds\":42}"), heartbeat_json(42, b, sizeof(b)));
    TEST_ASSERT_EQUAL_STRING("{\"subsystem\":\"vending\",\"uptime_seconds\":42}", b);
    golden("heartbeat", b);
    lwt_json(b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"subsystem\":\"vending\",\"uptime_seconds\":-1}", b);
    golden("lwt", b);
}

static Ack make_ack(AckStatus s, Phase p, const char* detail, const char* result) {
    Ack a;
    memset(&a, 0, sizeof(a));
    snprintf(a.request_id, sizeof(a.request_id), "req-0001");
    snprintf(a.command, sizeof(a.command), "dispense");
    a.status = s;
    a.phase = p;
    snprintf(a.detail, sizeof(a.detail), "%s", detail);
    snprintf(a.result_json, sizeof(a.result_json), "%s", result);
    return a;
}

static void test_acks(void) {
    char b[600];
    ack_json(make_ack(AckStatus::Ok, Phase::Accepted, "", ""), b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"request_id\":\"req-0001\",\"command\":\"dispense\",\"status\":\"ok\",\"phase\":\"accepted\"}", b);
    golden("ack_ok_accepted", b);

    ack_json(make_ack(AckStatus::Ok, Phase::Completed, "", "{\"checks\":[{\"name\":\"io\",\"pass\":true}]}"), b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"phase\":\"completed\",\"result\":{\"checks\":[{\"name\":\"io\",\"pass\":true}]}}"));
    TEST_ASSERT_NULL(strstr(b, "detail"));
    golden("ack_ok_completed", b);

    ack_json(make_ack(AckStatus::Rejected, Phase::Completed, "slot busy", ""), b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"status\":\"rejected\""));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"detail\":\"slot busy\""));
    TEST_ASSERT_NULL(strstr(b, "result"));
    golden("ack_rejected", b);

    ack_json(make_ack(AckStatus::Failed, Phase::Completed, "a \"quoted\" back\\slash", ""), b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"detail\":\"a \\\"quoted\\\" back\\\\slash\""));
    golden("ack_failed", b);

    ack_json(make_ack(AckStatus::Unsupported, Phase::Completed, "unknown command", ""), b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"status\":\"unsupported\""));
    golden("ack_unsupported", b);
}

static void test_event_payloads(void) {
    char b[400];
    Event e = ev(EventType::InputChanged, "sump_high");
    e.state = true;
    payload_for(e, ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"device\":\"sump_high\",\"state\":true}", b);
    golden("hardware_io", b);

    payload_for(ev(EventType::ButtonPressed, "button_ice"), ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"button\":\"button_ice\",\"action\":\"pressed\"}", b);
    golden("button_press", b);

    e = ev(EventType::Step, nullptr);
    e.slot = 1;
    snprintf(e.text, sizeof(e.text), "%s", step_name(Step::Agitate));
    snprintf(e.request_id, sizeof(e.request_id), "req-0001");
    payload_for(e, ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"slot\":1,\"state\":\"agitate\",\"request_id\":\"req-0001\"}", b);
    golden("dispenser_step", b);

    e = ev(EventType::Outcome, nullptr);
    e.slot = 1;
    e.value = static_cast<uint32_t>(Outcome::Jam);
    snprintf(e.text, sizeof(e.text), "stall");
    snprintf(e.request_id, sizeof(e.request_id), "req-0001");
    payload_for(e, ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"slot\":1,\"state\":\"jam\",\"request_id\":\"req-0001\",\"detail\":\"stall\"}", b);
    golden("dispenser_outcome", b);

    e = ev(EventType::Telemetry, "fill_pulses");
    e.value = 412;
    payload_for(e, ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"channel_id\":\"fill_pulses\",\"value\":412}", b);
    golden("telemetry_fill_pulses", b);

    e = ev(EventType::Temperature, "ambient");
    e.fvalue = 21.5f;
    payload_for(e, ctx(), 0, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING("{\"location\":\"ambient\",\"value\":21.50,\"unit\":\"C\"}", b);
    golden("sensor_ambient", b);

    WireCtx timed = ctx();
    timed.iso_time = "2026-10-18T12:00:00Z";
    payload_for(e, timed, 0, b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, ",\"timestamp\":\"2026-10-18T12:00:00Z\"}"));
    TEST_ASSERT_EQUAL_UINT(0, payload_for(ev(EventType::Health, nullptr), ctx(), 0, b, sizeof(b)));
}

static void test_ha_discovery(void) {
    // 6 non-button inputs + 7 outputs + ambient + uptime.
    TEST_ASSERT_EQUAL_INT(15, ha_discovery_count());
    char t[160], p[800];
    for (int i = 0; i < ha_discovery_count(); ++i) {
        TEST_ASSERT_GREATER_THAN_UINT(0, ha_discovery(i, ctx(), t, sizeof(t), p, sizeof(p)));
        TEST_ASSERT_NOT_NULL(strstr(t, "homeassistant/"));
        TEST_ASSERT_NULL(strstr(t, "button_"));
    }
    TEST_ASSERT_EQUAL_UINT(0, ha_discovery(ha_discovery_count(), ctx(), t, sizeof(t), p, sizeof(p)));
    ha_discovery(0, ctx(), t, sizeof(t), p, sizeof(p));
    TEST_ASSERT_EQUAL_STRING("homeassistant/binary_sensor/vm-01_vending/bag_full_sensor/config", t);
    TEST_ASSERT_NOT_NULL(strstr(p, "\"state_topic\":\"vmc/vm-01/hardware/io/bag_full_sensor\""));
    golden("ha_discovery", p);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_topics);
    RUN_TEST(test_capabilities);
    RUN_TEST(test_heartbeat_and_lwt);
    RUN_TEST(test_acks);
    RUN_TEST(test_event_payloads);
    RUN_TEST(test_ha_discovery);
    return UNITY_END();
}
