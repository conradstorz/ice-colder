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

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_capabilities);
    RUN_TEST(test_heartbeat_and_lwt);
    return UNITY_END();
}
