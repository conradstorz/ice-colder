#include <string.h>
#include <unity.h>

#include "version.h"

void setUp(void) {}
void tearDown(void) {}

static void test_contract_version(void) {
    TEST_ASSERT_EQUAL_STRING("2.0.0", vendcore::kContractVersion);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_contract_version);
    return UNITY_END();
}
