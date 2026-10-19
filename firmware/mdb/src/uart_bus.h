// The MDB serial bus: UART1 at 9600 baud, 8 data bits plus the mode bit (the UART's parity bit)
// plus one stop bit, behind mdbcore::BusPort.
//
// TX: the parity bit is the mode bit, so for each byte the port waits for the previous byte to
// leave the shifter, selects EVEN or ODD parity with parity_even_for(data, mode), and writes the
// byte alone. RX: the UART is configured for EVEN parity, so the hardware raises a parity error
// exactly when the received ninth bit differs from popcount(data) & 1; the ninth bit (the mode
// bit) is therefore  (popcount(data) & 1) ^ parity_error.  The IDF driver reports each error as a
// UART_PARITY_ERR event queued right AFTER the UART_DATA event of its byte, so a DATA event of one
// byte followed immediately by a PARITY_ERR gives that byte's mode exactly. Anything that cannot be
// attributed to a single byte falls back to mode = false and is counted in ambiguous(); the Link
// then ends the block on the 1 ms idle gap and counts the disagreement itself.
#pragma once

#include <stdint.h>

#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "ports.h"

class UartBus : public mdbcore::BusPort {
public:
    // Installs the driver. False (and logs) when the UART could not be configured.
    bool begin(uart_port_t port, int tx_gpio, int rx_gpio);

    void write(const uint8_t* bytes, const bool* modes, uint8_t n) override;
    bool read(mdbcore::RxByte& out, uint32_t timeout_us) override;
    void flush_rx() override;

    // Multi-byte DATA events with a parity error inside: modes are unknown, fell back to false.
    uint32_t ambiguous() const { return ambiguous_; }
    // PARITY_ERR events that matched no byte at all (late or lost).
    uint32_t orphans() const { return orphans_; }
    // Driver-level losses: FIFO overflow, ring buffer full, framing errors.
    uint32_t rx_lost() const { return rx_lost_; }

private:
    static constexpr int kFifo = 64;

    void pump(uint32_t wait_ticks);
    void on_data(uint32_t size);
    void push(uint8_t data, bool mode);

    uart_port_t port_ = UART_NUM_1;
    QueueHandle_t events_ = nullptr;
    mdbcore::RxByte fifo_[kFifo] = {};
    int head_ = 0;
    int count_ = 0;
    uart_event_t stash_ = {};
    bool have_stash_ = false;
    uint32_t ambiguous_ = 0;
    uint32_t orphans_ = 0;
    uint32_t rx_lost_ = 0;
};
