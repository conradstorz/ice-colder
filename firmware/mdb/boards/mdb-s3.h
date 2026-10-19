// ESP32-S3-DevKitC-1 pin map for the MDB payment board (spec section 4.3).
#pragma once

#include "driver/uart.h"

namespace mdbboard {
constexpr uart_port_t kMdbUart = UART_NUM_1;
constexpr int kMdbTxGpio = 17;
constexpr int kMdbRxGpio = 18;
constexpr int kStatusLedGpio = 48;
}  // namespace mdbboard
