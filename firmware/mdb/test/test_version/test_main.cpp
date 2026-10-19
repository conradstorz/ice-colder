#include <string.h>
#include <unity.h>

#include "version.h"

void setUp(void) {}
void tearDown(void) {}

static void test_versions(void) {
    TEST_ASSERT_EQUAL_STRING("1.1.0", mdbcore::kContractVersion);
    TEST_ASSERT_EQUAL_STRING("0.1.0", mdbcore::kFirmwareVersion);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_versions);
    return UNITY_END();
}
