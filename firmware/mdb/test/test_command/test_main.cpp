#include <initializer_list>
#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "command.h"

using namespace mdbcore;

void setUp(void) {}
void tearDown(void) {}

static Decoded dec(const char* json) { return decode_command(json, strlen(json)); }

#define RID "\"request_id\":\"req-00000001\""

static void expect_rejected(const char* json, const char* field) {
    const Decoded d = dec(json);
    TEST_ASSERT_EQUAL_MESSAGE(static_cast<int>(Decode::Rejected), static_cast<int>(d.result), json);
    TEST_ASSERT_NOT_NULL_MESSAGE(strstr(d.detail, field), d.detail);
}

static void test_standard_commands(void) {
    const char* names[3] = {"ping", "self_test", "force_report"};
    const CmdKind kinds[3] = {CmdKind::Ping, CmdKind::SelfTest, CmdKind::ForceReport};
    for (int i = 0; i < 3; ++i) {
        char j[128];
        snprintf(j, sizeof(j), "{\"command\":\"%s\"," RID "}", names[i]);
        const Decoded d = dec(j);
        TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
        TEST_ASSERT_EQUAL(static_cast<int>(kinds[i]), static_cast<int>(d.cmd.kind));
        TEST_ASSERT_EQUAL_STRING(names[i], d.cmd.name);
        TEST_ASSERT_EQUAL_STRING("req-00000001", d.cmd.request_id);
        TEST_ASSERT_EQUAL_INT64(-1, d.cmd.timestamp_unix);
    }
    const Decoded r = dec("{\"command\":\"return_escrow\"," RID ",\"params\":{}}");
    TEST_ASSERT_EQUAL(static_cast<int>(CmdKind::ReturnEscrow), static_cast<int>(r.cmd.kind));
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(r.result));
}

static void test_set_policy(void) {
    const Decoded d = dec(
        "{\"command\":\"set_policy\"," RID ",\"params\":{\"policy_id\":\"p-1\",\"accept\":true,"
        "\"max_escrow_cents\":5000,\"coin_enabled\":[5,10,25,100],\"bill_enabled\":[100,500],"
        "\"cashless_enabled\":false,\"change_threshold_cents\":150,\"edge_timeout_s\":45,"
        "\"return_bill_on_lever\":true}}");
    TEST_ASSERT_EQUAL_STRING(d.detail, "");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    const Policy& p = d.cmd.policy;
    TEST_ASSERT_EQUAL_STRING("p-1", p.policy_id);
    TEST_ASSERT_TRUE(p.accept);
    TEST_ASSERT_EQUAL_UINT32(5000, p.max_escrow_cents);
    TEST_ASSERT_EQUAL_UINT8(4, p.n_coin);
    TEST_ASSERT_EQUAL_UINT32(25, p.coin_enabled[2]);
    TEST_ASSERT_EQUAL_UINT8(2, p.n_bill);
    TEST_ASSERT_EQUAL_UINT32(500, p.bill_enabled[1]);
    TEST_ASSERT_FALSE(p.cashless_enabled);
    TEST_ASSERT_EQUAL_UINT32(150, p.change_threshold_cents);
    TEST_ASSERT_EQUAL_UINT16(45, p.edge_timeout_s);
    TEST_ASSERT_TRUE(p.return_bill_on_lever);
}

static void test_set_policy_max_arrays_fit_the_arena(void) {
    char j[1024];
    int n = snprintf(j, sizeof(j),
                     "{\"command\":\"set_policy\"," RID ",\"timestamp\":\"2026-10-18T12:00:00Z\","
                     "\"params\":{\"policy_id\":\"p-1\",\"accept\":true,\"max_escrow_cents\":5000,"
                     "\"coin_enabled\":[");
    for (int i = 1; i <= 16; ++i) n += snprintf(j + n, sizeof(j) - n, "%s%d", i > 1 ? "," : "", i * 5);
    n += snprintf(j + n, sizeof(j) - n, "],\"bill_enabled\":[");
    for (int i = 1; i <= 16; ++i) n += snprintf(j + n, sizeof(j) - n, "%s%d", i > 1 ? "," : "", i * 100);
    snprintf(j + n, sizeof(j) - n,
             "],\"cashless_enabled\":true,\"change_threshold_cents\":0,\"edge_timeout_s\":30,"
             "\"return_bill_on_lever\":false}}");
    const Decoded d = dec(j);
    TEST_ASSERT_EQUAL_STRING("", d.detail);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    TEST_ASSERT_EQUAL_UINT8(16, d.cmd.policy.n_coin);
    TEST_ASSERT_EQUAL_UINT8(16, d.cmd.policy.n_bill);
    TEST_ASSERT_EQUAL_UINT32(1600, d.cmd.policy.bill_enabled[15]);
}

static void test_set_policy_bounds(void) {
#define POL(field_override)                                                                      \
    "{\"command\":\"set_policy\"," RID ",\"params\":{" field_override "}}"
    const char* base_tail = ",\"accept\":true,\"max_escrow_cents\":5000,\"coin_enabled\":[5],"
                            "\"bill_enabled\":[100],\"cashless_enabled\":true,"
                            "\"change_threshold_cents\":0,\"edge_timeout_s\":30,"
                            "\"return_bill_on_lever\":false";
    char j[600];
    snprintf(j, sizeof(j), POL("\"policy_id\":\"\"%s"), base_tail);
    expect_rejected(j, "policy_id");
    char longid[80];
    memset(longid, 'x', 65);
    longid[65] = '\0';
    snprintf(j, sizeof(j), POL("\"policy_id\":\"%s\"%s"), longid, base_tail);
    expect_rejected(j, "policy_id");
    snprintf(j, sizeof(j), POL("\"policy_id\":\"p\"%s"), "");
    expect_rejected(j, "accept");

    const char* head = "\"policy_id\":\"p\",\"accept\":true,";
    snprintf(j, sizeof(j), POL("%s\"max_escrow_cents\":0,\"coin_enabled\":[],\"bill_enabled\":[],"
                               "\"cashless_enabled\":true,\"change_threshold_cents\":0,"
                               "\"edge_timeout_s\":30,\"return_bill_on_lever\":false"), head);
    expect_rejected(j, "max_escrow_cents");
    snprintf(j, sizeof(j), POL("%s\"max_escrow_cents\":1,\"coin_enabled\":[5,0],\"bill_enabled\":[],"
                               "\"cashless_enabled\":true,\"change_threshold_cents\":0,"
                               "\"edge_timeout_s\":30,\"return_bill_on_lever\":false"), head);
    expect_rejected(j, "coin_enabled");
    snprintf(j, sizeof(j), POL("%s\"max_escrow_cents\":1,\"coin_enabled\":[],\"bill_enabled\":[1.5],"
                               "\"cashless_enabled\":true,\"change_threshold_cents\":0,"
                               "\"edge_timeout_s\":30,\"return_bill_on_lever\":false"), head);
    expect_rejected(j, "bill_enabled");
    for (const char* bad : {"4", "121", "30.0", "\"30\""}) {
        char tail[200];
        snprintf(tail, sizeof(tail),
                 "%s\"max_escrow_cents\":1,\"coin_enabled\":[],\"bill_enabled\":[],"
                 "\"cashless_enabled\":true,\"change_threshold_cents\":0,"
                 "\"edge_timeout_s\":%s,\"return_bill_on_lever\":false", head, bad);
        snprintf(j, sizeof(j), POL("%s"), tail);
        expect_rejected(j, "edge_timeout_s");
    }
    for (const char* ok : {"5", "120"}) {
        char tail[200];
        snprintf(tail, sizeof(tail),
                 "%s\"max_escrow_cents\":1,\"coin_enabled\":[],\"bill_enabled\":[],"
                 "\"cashless_enabled\":true,\"change_threshold_cents\":0,"
                 "\"edge_timeout_s\":%s,\"return_bill_on_lever\":false", head, ok);
        snprintf(j, sizeof(j), POL("%s"), tail);
        TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(dec(j).result));
    }
    snprintf(j, sizeof(j), POL("%s\"max_escrow_cents\":1,\"coin_enabled\":[],\"bill_enabled\":[],"
                               "\"cashless_enabled\":\"yes\",\"change_threshold_cents\":0,"
                               "\"edge_timeout_s\":30,\"return_bill_on_lever\":false"), head);
    expect_rejected(j, "cashless_enabled");
    snprintf(j, sizeof(j), POL("%s\"max_escrow_cents\":1,\"coin_enabled\":[],\"bill_enabled\":[],"
                               "\"cashless_enabled\":true,\"change_threshold_cents\":-1,"
                               "\"edge_timeout_s\":30,\"return_bill_on_lever\":false"), head);
    expect_rejected(j, "change_threshold_cents");
#undef POL
}

static void test_payout(void) {
    const Decoded d = dec("{\"command\":\"payout\"," RID
                          ",\"params\":{\"cents\":75,\"reason\":\"vend_failed\",\"sale_id\":\"s-9\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    TEST_ASSERT_EQUAL(static_cast<int>(CmdKind::Payout), static_cast<int>(d.cmd.kind));
    TEST_ASSERT_EQUAL_UINT32(75, d.cmd.cents);
    TEST_ASSERT_EQUAL_STRING("vend_failed", d.cmd.reason);
    TEST_ASSERT_EQUAL_STRING("s-9", d.cmd.sale_id);
    const Decoded n = dec("{\"command\":\"payout\"," RID
                          ",\"params\":{\"cents\":75,\"reason\":\"change\",\"sale_id\":null}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(n.result));
    TEST_ASSERT_EQUAL_STRING("", n.cmd.sale_id);
    for (const char* r : {"change", "session_timeout", "cancel", "vend_failed", "maintenance",
                          "admin", "error", "customer"}) {
        char j[200];
        snprintf(j, sizeof(j), "{\"command\":\"payout\"," RID ",\"params\":{\"cents\":5,\"reason\":\"%s\"}}", r);
        TEST_ASSERT_EQUAL_MESSAGE(static_cast<int>(Decode::Ok), static_cast<int>(dec(j).result), r);
    }
    expect_rejected("{\"command\":\"payout\"," RID ",\"params\":{\"cents\":75,\"reason\":\"refund\"}}", "reason");
    expect_rejected("{\"command\":\"payout\"," RID ",\"params\":{\"cents\":0,\"reason\":\"change\"}}", "cents");
    expect_rejected("{\"command\":\"payout\"," RID ",\"params\":{\"reason\":\"change\"}}", "cents");
}

static void test_cents_is_integer_only(void) {
    for (const char* bad : {"1.5", "100.0", "\"100\"", "true", "-5", "null", "1e2", "4294967296"}) {
        char j[200];
        snprintf(j, sizeof(j), "{\"command\":\"payout\"," RID ",\"params\":{\"cents\":%s,\"reason\":\"change\"}}", bad);
        expect_rejected(j, "cents");
        snprintf(j, sizeof(j), "{\"command\":\"cash_sale\"," RID ",\"params\":{\"cents\":%s}}", bad);
        expect_rejected(j, "cents");
        snprintf(j, sizeof(j), "{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":%s,\"item\":\"3\"}}", bad);
        expect_rejected(j, "cents");
    }
}

static void test_cash_sale(void) {
    const Decoded d = dec("{\"command\":\"cash_sale\"," RID ",\"params\":{\"cents\":200,\"sale_id\":\"x\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    TEST_ASSERT_EQUAL(static_cast<int>(CmdKind::CashSale), static_cast<int>(d.cmd.kind));
    TEST_ASSERT_EQUAL_UINT32(200, d.cmd.cents);
    TEST_ASSERT_EQUAL_STRING("x", d.cmd.sale_id);
}

static void test_vend_request(void) {
    const Decoded d = dec("{\"command\":\"vend_request\"," RID
                          ",\"params\":{\"cents\":125,\"item\":\"A3\",\"session_id\":\"sess-1\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    TEST_ASSERT_EQUAL_UINT32(125, d.cmd.cents);
    TEST_ASSERT_EQUAL_STRING("A3", d.cmd.item);
    TEST_ASSERT_TRUE(d.cmd.has_session_id);
    TEST_ASSERT_EQUAL_STRING("sess-1", d.cmd.session_id);
    const Decoded n = dec("{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125,\"item\":\"A3\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(n.result));
    TEST_ASSERT_FALSE(n.cmd.has_session_id);
    const Decoded z = dec("{\"command\":\"vend_request\"," RID
                          ",\"params\":{\"cents\":125,\"item\":\"A3\",\"session_id\":null}}");
    TEST_ASSERT_FALSE(z.cmd.has_session_id);
    expect_rejected("{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125,\"item\":\"\"}}", "item");
    char j[300];
    char item[80];
    memset(item, 'i', 65);
    item[65] = '\0';
    snprintf(j, sizeof(j), "{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125,\"item\":\"%s\"}}", item);
    expect_rejected(j, "item");
    item[64] = '\0';
    snprintf(j, sizeof(j), "{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125,\"item\":\"%s\"}}", item);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(dec(j).result));
    expect_rejected("{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125,\"item\":3}}", "item");
    expect_rejected("{\"command\":\"vend_request\"," RID ",\"params\":{\"cents\":125}}", "item");
}

static void test_vend_result(void) {
    const Decoded d = dec("{\"command\":\"vend_result\"," RID
                          ",\"params\":{\"session_id\":\"sess-1\",\"request_id\":\"vend-0000001\","
                          "\"outcome\":\"success\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
    TEST_ASSERT_TRUE(d.cmd.has_session_id);
    TEST_ASSERT_EQUAL_STRING("sess-1", d.cmd.session_id);
    TEST_ASSERT_EQUAL_STRING("vend-0000001", d.cmd.vend_request_id);
    TEST_ASSERT_TRUE(d.cmd.success);
    const Decoded f = dec("{\"command\":\"vend_result\"," RID
                          ",\"params\":{\"session_id\":\"s\",\"request_id\":\"vend-0000001\","
                          "\"outcome\":\"failure\"}}");
    TEST_ASSERT_FALSE(f.cmd.success);
    expect_rejected("{\"command\":\"vend_result\"," RID
                    ",\"params\":{\"session_id\":\"s\",\"request_id\":\"short\",\"outcome\":\"success\"}}",
                    "request_id");
    expect_rejected("{\"command\":\"vend_result\"," RID
                    ",\"params\":{\"session_id\":\"s\",\"request_id\":\"vend-0000001\",\"outcome\":\"ok\"}}",
                    "outcome");
    expect_rejected("{\"command\":\"vend_result\"," RID
                    ",\"params\":{\"request_id\":\"vend-0000001\",\"outcome\":\"success\"}}",
                    "session_id");
}

static void test_end_session_and_device_reset(void) {
    const Decoded e = dec("{\"command\":\"end_session\"," RID ",\"params\":{\"session_id\":\"sess-1\"}}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(e.result));
    TEST_ASSERT_TRUE(e.cmd.has_session_id);
    TEST_ASSERT_EQUAL_STRING("sess-1", e.cmd.session_id);
    expect_rejected("{\"command\":\"end_session\"," RID ",\"params\":{\"session_id\":\"\"}}", "session_id");
    expect_rejected("{\"command\":\"end_session\"," RID "}", "session_id");

    const char* devs[3] = {"coin", "bill", "cashless"};
    const Device want[3] = {Device::Coin, Device::Bill, Device::Cashless};
    for (int i = 0; i < 3; ++i) {
        char j[160];
        snprintf(j, sizeof(j), "{\"command\":\"device_reset\"," RID ",\"params\":{\"device\":\"%s\"}}", devs[i]);
        const Decoded d = dec(j);
        TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
        TEST_ASSERT_EQUAL(static_cast<int>(want[i]), static_cast<int>(d.cmd.device));
    }
    expect_rejected("{\"command\":\"device_reset\"," RID ",\"params\":{\"device\":\"stacker\"}}", "device");
}

static void test_request_id_and_unsupported(void) {
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId), static_cast<int>(dec("{\"command\":\"ping\"}").result));
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId),
                      static_cast<int>(dec("{\"command\":\"ping\",\"request_id\":\"short\"}").result));
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId),
                      static_cast<int>(dec("{\"command\":\"ping\",\"request_id\":12345678}").result));
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId), static_cast<int>(dec("not json").result));
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId), static_cast<int>(dec("[1,2]").result));
    char j[200];
    char rid[80];
    memset(rid, 'r', 65);
    rid[65] = '\0';
    snprintf(j, sizeof(j), "{\"command\":\"ping\",\"request_id\":\"%s\"}", rid);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::NoRequestId), static_cast<int>(dec(j).result));
    rid[64] = '\0';
    snprintf(j, sizeof(j), "{\"command\":\"ping\",\"request_id\":\"%s\"}", rid);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(dec(j).result));

    const Decoded u = dec("{\"command\":\"dispense\"," RID "}");
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Unsupported), static_cast<int>(u.result));
    TEST_ASSERT_EQUAL_STRING("req-00000001", u.cmd.request_id);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Unsupported), static_cast<int>(dec("{" RID "}").result));
}

static void test_timestamp(void) {
    Decoded d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":\"2026-10-18T12:34:56Z\"}");
    TEST_ASSERT_EQUAL_INT64(1792326896, d.cmd.timestamp_unix);
    d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":\"2026-10-18T12:34:56.789+02:00\"}");
    TEST_ASSERT_EQUAL_INT64(1792326896 - 7200, d.cmd.timestamp_unix);
    d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":\"2026-10-18T12:34:56-05:30\"}");
    TEST_ASSERT_EQUAL_INT64(1792326896 + 19800, d.cmd.timestamp_unix);
    d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":\"1970-01-01T00:00:00Z\"}");
    TEST_ASSERT_EQUAL_INT64(0, d.cmd.timestamp_unix);
    d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":\"yesterday\"}");
    TEST_ASSERT_EQUAL_INT64(-1, d.cmd.timestamp_unix);
    d = dec("{\"command\":\"ping\"," RID ",\"timestamp\":12}");
    TEST_ASSERT_EQUAL_INT64(-1, d.cmd.timestamp_unix);
    TEST_ASSERT_EQUAL(static_cast<int>(Decode::Ok), static_cast<int>(d.result));
}

static void test_decode_event_ack(void) {
    char b[40];
    uint32_t seq = 0;
    const char* ok = "{\"boot_id\":\"0123456789abcdef\",\"seq\":17,\"timestamp\":\"2026-10-18T12:00:00Z\"}";
    TEST_ASSERT_TRUE(decode_event_ack(ok, strlen(ok), b, seq));
    TEST_ASSERT_EQUAL_STRING("0123456789abcdef", b);
    TEST_ASSERT_EQUAL_UINT32(17, seq);
    for (const char* bad : {"{\"boot_id\":\"short\",\"seq\":1}", "{\"boot_id\":\"0123456789abcdef\",\"seq\":0}",
                            "{\"boot_id\":\"0123456789abcdef\",\"seq\":1.5}",
                            "{\"boot_id\":\"0123456789abcdef\",\"seq\":\"1\"}",
                            "{\"boot_id\":\"0123456789abcdef\"}", "{\"seq\":1}", "garbage",
                            "{\"boot_id\":\"0123456789abcdef0123456789abcdef0\",\"seq\":1}"}) {
        TEST_ASSERT_FALSE_MESSAGE(decode_event_ack(bad, strlen(bad), b, seq), bad);
    }
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_standard_commands);
    RUN_TEST(test_set_policy);
    RUN_TEST(test_set_policy_max_arrays_fit_the_arena);
    RUN_TEST(test_set_policy_bounds);
    RUN_TEST(test_payout);
    RUN_TEST(test_cents_is_integer_only);
    RUN_TEST(test_cash_sale);
    RUN_TEST(test_vend_request);
    RUN_TEST(test_vend_result);
    RUN_TEST(test_end_session_and_device_reset);
    RUN_TEST(test_request_id_and_unsupported);
    RUN_TEST(test_timestamp);
    RUN_TEST(test_decode_event_ack);
    return UNITY_END();
}
