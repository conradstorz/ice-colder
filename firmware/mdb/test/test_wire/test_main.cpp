#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "wire.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

static WireCtx ctx() {
    WireCtx c;
    c.machine_id = "vm-01";
    c.hardware_id = "esp32-a0b1c2d3e4f5";
    c.ip = "192.168.1.60";
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

static void test_capabilities(void) {
    static char buf[1024];
    const size_t n = capabilities_json(ctx(), buf, sizeof(buf));
    TEST_ASSERT_GREATER_THAN_UINT(0, n);
    TEST_ASSERT_EQUAL_UINT(strlen(buf), n);
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"subsystem\":\"mdb\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"contract_version\":\"1.1.0\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"channels\":[]"));
    TEST_ASSERT_NOT_NULL(strstr(
        buf, "\"commands\":[\"ping\",\"self_test\",\"force_report\",\"set_policy\",\"payout\",\"vend_request\","
             "\"vend_result\",\"end_session\",\"return_escrow\",\"device_reset\",\"cash_sale\"]"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"hardware_id\":\"esp32-a0b1c2d3e4f5\""));
    TEST_ASSERT_NULL(strstr(buf, "timestamp"));
    char small[64];
    TEST_ASSERT_EQUAL_UINT(0, capabilities_json(ctx(), small, sizeof(small)));
    golden("capabilities", buf);
}

static void test_heartbeat_and_lwt(void) {
    char b[96];
    TEST_ASSERT_GREATER_THAN_UINT(0, heartbeat_json(42, b, sizeof(b)));
    TEST_ASSERT_EQUAL_STRING("{\"subsystem\":\"mdb\",\"uptime_seconds\":42}", b);
    golden("heartbeat", b);
    TEST_ASSERT_GREATER_THAN_UINT(0, lwt_json(b, sizeof(b)));
    TEST_ASSERT_EQUAL_STRING("{\"subsystem\":\"mdb\",\"uptime_seconds\":-1}", b);
    golden("lwt", b);
}

static const char* kTs = "2026-10-18T12:34:56Z";
static const char* kBoot = "0123456789abcdef";

static PaymentEvent ev(EventKind k, Device d = Device::Coin) { return make_event(k, d); }

// Parses `json` with ArduinoJson and checks that the event object has exactly `keys` (space
// separated, "kind" implied), then returns the document via `doc`.
static void expect_event_keys(JsonDocument& doc, const char* json, const char* kind, const char* keys) {
    TEST_ASSERT_TRUE_MESSAGE(deserializeJson(doc, json) == DeserializationError::Ok, json);
    TEST_ASSERT_EQUAL_STRING("0123456789abcdef-7", doc["event_id"].as<const char*>());
    TEST_ASSERT_EQUAL_STRING(kBoot, doc["boot_id"].as<const char*>());
    TEST_ASSERT_EQUAL_UINT32(7, doc["seq"].as<uint32_t>());
    TEST_ASSERT_EQUAL_STRING(kTs, doc["ts"].as<const char*>());
    TEST_ASSERT_EQUAL_size_t(5, doc.as<JsonObject>().size());
    JsonObject e = doc["event"];
    TEST_ASSERT_EQUAL_STRING(kind, e["kind"].as<const char*>());
    char want[160];
    snprintf(want, sizeof(want), "kind %s", keys);
    size_t n = 0;
    for (JsonPair p : e) {
        ++n;
        char needle[48];
        snprintf(needle, sizeof(needle), " %s ", p.key().c_str());
        char padded[176];
        snprintf(padded, sizeof(padded), " %s ", want);
        TEST_ASSERT_NOT_NULL_MESSAGE(strstr(padded, needle), p.key().c_str());
    }
    size_t expected = keys[0] == 0 ? 1 : 2;
    for (const char* c = keys; *c != '\0'; ++c) {
        if (*c == ' ') ++expected;
    }
    TEST_ASSERT_EQUAL_size_t(expected, n);
}

static void emit(const char* name, const char* keys, const PaymentEvent& e, const char* kind) {
    static char buf[512];
    const size_t n = event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    TEST_ASSERT_GREATER_THAN_UINT(0, n);
    TEST_ASSERT_EQUAL_UINT(strlen(buf), n);
    JsonDocument doc;
    expect_event_keys(doc, buf, kind, keys);
    char tiny[40];
    TEST_ASSERT_EQUAL_UINT(0, event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, tiny, sizeof(tiny)));
    char g[64];
    snprintf(g, sizeof(g), "event_%s", name);
    golden(g, buf);
}

static void test_event_goldens(void) {
    PaymentEvent e = ev(EventKind::CoinAccepted);
    e.cents = 25;
    e.route = CoinRoute::Tubes;
    emit("coin_accepted", "cents routed", e, "coin_accepted");

    e = ev(EventKind::BillEscrowed, Device::Bill);
    e.cents = 500;
    emit("bill_escrowed", "cents", e, "bill_escrowed");

    e = ev(EventKind::BillStacked, Device::Bill);
    e.cents = 500;
    emit("bill_stacked", "cents", e, "bill_stacked");

    e = ev(EventKind::BillReturned, Device::Bill);
    e.cents = 500;
    e.bill_reason = BillReturnReason::EdgeSilent;
    emit("bill_returned", "cents reason", e, "bill_returned");

    emit("escrow_return_pressed", "", ev(EventKind::EscrowReturnPressed), "escrow_return_pressed");

    e = ev(EventKind::PayoutResult);
    snprintf(e.request_id, sizeof(e.request_id), "pay-00000010");
    e.requested_cents = 175;
    e.paid_cents = 150;
    e.payout_status = PayoutStatus::Partial;
    snprintf(e.detail, sizeof(e.detail), "tube empty");
    emit("payout_result", "request_id requested_cents paid_cents status detail", e, "payout_result");

    e = ev(EventKind::SessionBegun, Device::Cashless);
    snprintf(e.session_id, sizeof(e.session_id), "sess-0001");
    e.funds_known = true;
    e.funds_cents = 1000;
    emit("session_begun", "session_id funds_cents", e, "session_begun");

    e = ev(EventKind::VendApproved, Device::Cashless);
    snprintf(e.session_id, sizeof(e.session_id), "sess-0001");
    snprintf(e.request_id, sizeof(e.request_id), "vend-0000001");
    e.cents = 125;
    emit("vend_approved", "session_id request_id cents", e, "vend_approved");

    e = ev(EventKind::VendDenied, Device::Cashless);
    snprintf(e.session_id, sizeof(e.session_id), "sess-0001");
    snprintf(e.request_id, sizeof(e.request_id), "vend-0000001");
    snprintf(e.detail, sizeof(e.detail), "insufficient funds");
    emit("vend_denied", "session_id request_id reason", e, "vend_denied");

    e = ev(EventKind::SessionEnded, Device::Cashless);
    snprintf(e.session_id, sizeof(e.session_id), "sess-0001");
    e.end_reason = SessionEndReason::Complete;
    emit("session_ended", "session_id reason", e, "session_ended");

    e = ev(EventKind::DeviceStatus, Device::Bill);
    e.status = Status::Jammed;
    e.mdb_status = 12;
    emit("device_status", "device status mdb_status", e, "device_status");

    e = ev(EventKind::CashCollected, Device::Coin);
    emit("cash_collected", "device", e, "cash_collected");
}

static void test_event_nullables(void) {
    static char buf[512];
    JsonDocument doc;
    PaymentEvent e = ev(EventKind::SessionBegun, Device::Cashless);
    snprintf(e.session_id, sizeof(e.session_id), "s");
    event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    deserializeJson(doc, buf);
    TEST_ASSERT_TRUE(doc["event"]["funds_cents"].isNull());
    TEST_ASSERT_TRUE(doc["event"]["funds_cents"].is<JsonVariant>());

    e = ev(EventKind::DeviceStatus, Device::Coin);
    e.status = Status::Offline;
    event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    deserializeJson(doc, buf);
    TEST_ASSERT_TRUE(doc["event"]["mdb_status"].isNull());
    TEST_ASSERT_TRUE(doc["event"]["mdb_status"].is<JsonVariant>());

    e = ev(EventKind::PayoutResult);
    snprintf(e.request_id, sizeof(e.request_id), "pay-00000011");
    e.payout_status = PayoutStatus::Ok;
    event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    deserializeJson(doc, buf);
    TEST_ASSERT_FALSE(doc["event"]["detail"].is<JsonVariant>());

    // Every enum string is non-null and escaping survives a round trip.
    e = ev(EventKind::VendDenied, Device::Cashless);
    snprintf(e.detail, sizeof(e.detail), "a \"q\" b\\s");
    event_json(e, "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    deserializeJson(doc, buf);
    TEST_ASSERT_EQUAL_STRING("a \"q\" b\\s", doc["event"]["reason"].as<const char*>());
}

static void test_peek_boot_seq_matches_event_json(void) {
    static char buf[512];
    event_json(ev(EventKind::EscrowReturnPressed), "0123456789abcdef-7", kBoot, 7, kTs, buf, sizeof(buf));
    char boot[33];
    uint32_t seq = 0;
    TEST_ASSERT_TRUE(peek_boot_seq(buf, boot, seq));
    TEST_ASSERT_EQUAL_STRING(kBoot, boot);
    TEST_ASSERT_EQUAL_UINT32(7, seq);
}

static PaymentState base_state() {
    PaymentState s;
    memset(&s, 0, sizeof(s));
    s.boot_id = kBoot;
    s.seq_last = 12;
    s.inhibited = Inhibit::None;
    s.accepting = true;
    s.policy_id = "p-00000001";
    s.devices[0].present = true;
    s.devices[0].status = Status::Ready;
    s.devices[0].level = 3;
    snprintf(s.devices[0].model, sizeof(s.devices[0].model), "COINCO");
    snprintf(s.devices[0].serial, sizeof(s.devices[0].serial), "SN0001");
    const uint32_t coins[4] = {5, 10, 25, 100};
    memcpy(s.devices[0].denominations, coins, sizeof(coins));
    s.devices[0].n_denominations = 4;
    memcpy(s.devices[0].enabled, coins, sizeof(coins));
    s.devices[0].n_enabled = 4;
    s.devices[1].present = true;
    s.devices[1].status = Status::Ready;
    const uint32_t bills[3] = {100, 500, 1000};
    memcpy(s.devices[1].denominations, bills, sizeof(bills));
    s.devices[1].n_denominations = 3;
    memcpy(s.devices[1].enabled, bills, 2 * sizeof(uint32_t));
    s.devices[1].n_enabled = 2;
    s.devices[2].present = true;
    s.devices[2].status = Status::Disabled;
    s.devices[2].level = 1;
    s.tubes[0] = {5, 10, false};
    s.tubes[1] = {25, 20, true};
    s.n_tubes = 2;
    s.stacker_known = true;
    s.stacker_count = 14;
    return s;
}

static void test_state_json(void) {
    static char buf[2048];
    PaymentState s = base_state();
    s.escrow_cents = 175;
    s.bill_in_escrow = true;
    s.bill_in_escrow_cents = 500;
    s.session_open = true;
    snprintf(s.session_id, sizeof(s.session_id), "sess-0001");
    s.funds_known = true;
    s.funds_cents = 1000;
    s.session_started_ts = "2026-10-18T12:30:00Z";
    const size_t n = state_json(s, kTs, buf, sizeof(buf));
    TEST_ASSERT_GREATER_THAN_UINT(0, n);
    TEST_ASSERT_EQUAL_UINT(strlen(buf), n);
    JsonDocument doc;
    TEST_ASSERT_TRUE(deserializeJson(doc, buf) == DeserializationError::Ok);
    TEST_ASSERT_EQUAL_size_t(14, doc.as<JsonObject>().size());
    TEST_ASSERT_EQUAL_STRING("USD", doc["currency"].as<const char*>());
    TEST_ASSERT_TRUE(doc["accepting"].as<bool>());
    TEST_ASSERT_TRUE(doc["inhibited_reason"].isNull());
    TEST_ASSERT_EQUAL_STRING("p-00000001", doc["policy_id"].as<const char*>());
    TEST_ASSERT_EQUAL_UINT32(175, doc["escrow_cents"].as<uint32_t>());
    TEST_ASSERT_EQUAL_UINT32(500, doc["bill_in_escrow_cents"].as<uint32_t>());
    TEST_ASSERT_EQUAL_size_t(2, doc["tubes"].size());
    TEST_ASSERT_EQUAL_UINT32(14, doc["stacker"]["count"].as<uint32_t>());
    TEST_ASSERT_EQUAL_STRING("2026-10-18T12:30:00Z", doc["session"]["started_ts"].as<const char*>());
    TEST_ASSERT_EQUAL_UINT32(1000, doc["session"]["funds_cents"].as<uint32_t>());
    TEST_ASSERT_EQUAL_size_t(7, doc["devices"]["coin"].as<JsonObject>().size());
    TEST_ASSERT_EQUAL_size_t(2, doc["devices"]["bill"]["enabled"].size());
    TEST_ASSERT_TRUE(doc["devices"]["cashless"]["model"].isNull());
    TEST_ASSERT_EQUAL_size_t(0, doc["devices"]["cashless"]["denominations"].size());
    golden("state", buf);

    TEST_ASSERT_EQUAL_UINT(0, state_json(s, kTs, buf, 100));
}

static void test_state_inhibited(void) {
    static char buf[2048];
    PaymentState s = base_state();
    s.accepting = true;  // lies: state_json derives it from `inhibited`
    s.inhibited = Inhibit::EdgeSilent;
    s.policy_id = nullptr;
    s.stacker_known = false;
    s.exact_change = true;
    s.devices[1].enabled[2] = 5000;  // not a declared denomination: dropped
    s.devices[1].n_enabled = 3;
    s.tubes[2] = {0, 3, false};  // cents == 0: omitted
    s.n_tubes = 3;
    TEST_ASSERT_GREATER_THAN_UINT(0, state_json(s, kTs, buf, sizeof(buf)));
    JsonDocument doc;
    TEST_ASSERT_TRUE(deserializeJson(doc, buf) == DeserializationError::Ok);
    TEST_ASSERT_FALSE(doc["accepting"].as<bool>());
    TEST_ASSERT_EQUAL_STRING("edge_silent", doc["inhibited_reason"].as<const char*>());
    TEST_ASSERT_TRUE(doc["policy_id"].isNull());
    TEST_ASSERT_TRUE(doc["stacker"].isNull());
    TEST_ASSERT_TRUE(doc["session"].isNull());
    TEST_ASSERT_TRUE(doc["bill_in_escrow_cents"].isNull());
    TEST_ASSERT_TRUE(doc["exact_change"].as<bool>());
    TEST_ASSERT_EQUAL_size_t(2, doc["tubes"].size());
    TEST_ASSERT_EQUAL_size_t(2, doc["devices"]["bill"]["enabled"].size());
    golden("state_inhibited", buf);

    const Inhibit all[5] = {Inhibit::None, Inhibit::NoPolicy, Inhibit::EdgeSilent, Inhibit::PolicyOff,
                            Inhibit::DeviceFault};
    const char* want[5] = {nullptr, "no_policy", "edge_silent", "policy", "device_fault"};
    for (int i = 0; i < 5; ++i) {
        if (want[i] == nullptr) {
            TEST_ASSERT_NULL(inhibit_str(all[i]));
        } else {
            TEST_ASSERT_EQUAL_STRING(want[i], inhibit_str(all[i]));
        }
    }
}

static void test_topics(void) {
    char t[64];
    const WireCtx c = ctx();
    topic_event(c, t, sizeof(t));
    TEST_ASSERT_EQUAL_STRING("vmc/vm-01/payment/event", t);
    topic_state(c, t, sizeof(t));
    TEST_ASSERT_EQUAL_STRING("vmc/vm-01/payment/state", t);
    topic_event_ack(c, t, sizeof(t));
    TEST_ASSERT_EQUAL_STRING("vmc/vm-01/payment/event/ack", t);
    topic_cmd(c, t, sizeof(t));
    TEST_ASSERT_EQUAL_STRING("vmc/vm-01/cmd/mdb", t);
    topic_ack(c, t, sizeof(t));
    TEST_ASSERT_EQUAL_STRING("vmc/vm-01/cmd/mdb/ack", t);
}

static boardcore::Ack make_ack(boardcore::AckStatus s, boardcore::Phase p, const char* cmd,
                               const char* detail, const char* result) {
    boardcore::Ack a;
    memset(&a, 0, sizeof(a));
    snprintf(a.request_id, sizeof(a.request_id), "req-00000001");
    snprintf(a.command, sizeof(a.command), "%s", cmd);
    a.status = s;
    a.phase = p;
    snprintf(a.detail, sizeof(a.detail), "%s", detail);
    snprintf(a.result_json, sizeof(a.result_json), "%s", result);
    return a;
}

static void test_acks(void) {
    using boardcore::AckStatus;
    using boardcore::Phase;
    char b[600];
    boardcore::ack_json(make_ack(AckStatus::Ok, Phase::Accepted, "payout", "", ""), b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING(
        "{\"request_id\":\"req-00000001\",\"command\":\"payout\",\"status\":\"ok\",\"phase\":\"accepted\"}", b);
    golden("ack_ok_accepted", b);
    boardcore::ack_json(make_ack(AckStatus::Ok, Phase::Completed, "self_test", "",
                                 "{\"checks\":[{\"name\":\"link\",\"pass\":true}]}"), b, sizeof(b));
    TEST_ASSERT_NOT_NULL(strstr(b, "\"result\":{\"checks\":[{\"name\":\"link\",\"pass\":true}]}"));
    golden("ack_ok_completed", b);
    boardcore::ack_json(make_ack(AckStatus::Rejected, Phase::Completed, "payout", "busy", ""), b, sizeof(b));
    golden("ack_rejected", b);
    boardcore::ack_json(make_ack(AckStatus::Failed, Phase::Completed, "set_policy", "device did not ack", ""), b, sizeof(b));
    golden("ack_failed", b);
    boardcore::ack_json(make_ack(AckStatus::Unsupported, Phase::Completed, "dispense", "unsupported command", ""), b, sizeof(b));
    golden("ack_unsupported", b);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_capabilities);
    RUN_TEST(test_heartbeat_and_lwt);
    RUN_TEST(test_event_goldens);
    RUN_TEST(test_event_nullables);
    RUN_TEST(test_peek_boot_seq_matches_event_json);
    RUN_TEST(test_state_json);
    RUN_TEST(test_state_inhibited);
    RUN_TEST(test_topics);
    RUN_TEST(test_acks);
    return UNITY_END();
}
