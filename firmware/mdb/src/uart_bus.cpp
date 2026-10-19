#include "uart_bus.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "hal/uart_ll.h"  // UART_INTR_* masks
#include "link.h"

namespace {
const char* TAG = "uart_bus";
constexpr int kRxRing = 512;
constexpr int kEventQueue = 32;

bool popcount_odd(uint8_t v) {
    v ^= static_cast<uint8_t>(v >> 4);
    v ^= static_cast<uint8_t>(v >> 2);
    v ^= static_cast<uint8_t>(v >> 1);
    return (v & 1) != 0;
}
}  // namespace

bool UartBus::begin(uart_port_t port, int tx_gpio, int rx_gpio) {
    port_ = port;
    uart_config_t cfg = {};
    cfg.baud_rate = 9600;
    cfg.data_bits = UART_DATA_8_BITS;
    cfg.parity = UART_PARITY_EVEN;  // RX: a parity error means mode != popcount(data) & 1
    cfg.stop_bits = UART_STOP_BITS_1;
    cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
    cfg.source_clk = UART_SCLK_DEFAULT;
    esp_err_t err = uart_param_config(port_, &cfg);
    if (err == ESP_OK) err = uart_set_pin(port_, tx_gpio, rx_gpio, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err == ESP_OK) err = uart_driver_install(port_, kRxRing, 0, kEventQueue, &events_, 0);
    if (err == ESP_OK) {
        uart_intr_config_t intr = {};
        intr.intr_enable_mask = UART_INTR_RXFIFO_FULL | UART_INTR_RXFIFO_TOUT | UART_INTR_PARITY_ERR |
                                UART_INTR_FRAM_ERR | UART_INTR_RXFIFO_OVF | UART_INTR_BRK_DET;
        intr.rx_timeout_thresh = 2;  // symbols
        intr.txfifo_empty_intr_thresh = 10;
        intr.rxfifo_full_thresh = 1;  // one interrupt per byte, so a DATA event is one byte
        err = uart_intr_config(port_, &intr);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart setup failed: %s", esp_err_to_name(err));
        return false;
    }
    return true;
}

void UartBus::write(const uint8_t* bytes, const bool* modes, uint8_t n) {
    for (uint8_t i = 0; i < n; ++i) {
        // Parity may only change once the previous byte has fully left the shifter.
        uart_wait_tx_done(port_, pdMS_TO_TICKS(20));
        uart_set_parity(port_, mdbcore::parity_even_for(bytes[i], modes[i]) ? UART_PARITY_EVEN : UART_PARITY_ODD);
        uart_write_bytes(port_, &bytes[i], 1);
    }
    uart_wait_tx_done(port_, pdMS_TO_TICKS(20));
}

void UartBus::push(uint8_t data, bool mode) {
    if (count_ >= kFifo) {
        ++rx_lost_;
        return;
    }
    mdbcore::RxByte& slot = fifo_[(head_ + count_) % kFifo];
    slot.data = data;
    slot.mode = mode;
    slot.t_us = static_cast<uint64_t>(esp_timer_get_time());
    ++count_;
}

void UartBus::on_data(uint32_t size) {
    uint8_t buf[128];
    const uint32_t want = size > sizeof(buf) ? static_cast<uint32_t>(sizeof(buf)) : size;
    const int got = uart_read_bytes(port_, buf, want, 0);
    if (got <= 0) return;
    // The PARITY_ERR for a byte is queued right after that byte's DATA event, and both are
    // normally posted before this task runs, so look ahead without blocking.
    int perr = 0;
    uart_event_t e;
    while (xQueueReceive(events_, &e, 0) == pdTRUE) {
        if (e.type == UART_PARITY_ERR) {
            ++perr;
        } else if (e.type == UART_DATA) {
            stash_ = e;
            have_stash_ = true;
            break;
        } else if (e.type == UART_FIFO_OVF || e.type == UART_BUFFER_FULL || e.type == UART_FRAME_ERR ||
                   e.type == UART_BREAK) {
            ++rx_lost_;
        }
    }
    if (got == 1) {
        push(buf[0], popcount_odd(buf[0]) != (perr > 0));
    } else if (perr == 0) {
        for (int i = 0; i < got; ++i) push(buf[i], popcount_odd(buf[i]));
    } else {
        ++ambiguous_;  // which byte of the batch carried the error is unknown: mode = false
        for (int i = 0; i < got; ++i) push(buf[i], false);
    }
}

void UartBus::pump(uint32_t wait_ticks) {
    uart_event_t ev;
    uint32_t wait = wait_ticks;
    for (;;) {
        if (have_stash_) {
            ev = stash_;
            have_stash_ = false;
        } else if (xQueueReceive(events_, &ev, wait) != pdTRUE) {
            return;
        }
        wait = 0;
        switch (ev.type) {
            case UART_DATA:
                on_data(ev.size);
                break;
            case UART_PARITY_ERR:
                // Arrived after the lookahead of the last byte: that byte's mode, if still queued.
                if (count_ > 0) {
                    mdbcore::RxByte& last = fifo_[(head_ + count_ - 1) % kFifo];
                    last.mode = !last.mode;
                } else {
                    ++orphans_;
                }
                break;
            case UART_FIFO_OVF:
            case UART_BUFFER_FULL:
                ++rx_lost_;
                uart_flush_input(port_);
                break;
            case UART_FRAME_ERR:
            case UART_BREAK:
                ++rx_lost_;
                break;
            default:
                break;
        }
        if (count_ > 0 && !have_stash_) return;  // hand the byte to the Link promptly
    }
}

bool UartBus::read(mdbcore::RxByte& out, uint32_t timeout_us) {
    const int64_t deadline = esp_timer_get_time() + static_cast<int64_t>(timeout_us);
    for (;;) {
        pump(0);
        if (count_ > 0) {
            out = fifo_[head_];
            head_ = (head_ + 1) % kFifo;
            --count_;
            return true;
        }
        const int64_t remaining = deadline - esp_timer_get_time();
        if (remaining <= 0) return false;
        // Block on the event queue while there is plenty of time (tick = 1 ms); spin for the last 3 ms.
        if (remaining > 3000) pump(static_cast<uint32_t>(remaining / 1000 - 2));
    }
}

void UartBus::flush_rx() {
    uart_flush_input(port_);
    xQueueReset(events_);
    have_stash_ = false;
    head_ = 0;
    count_ = 0;
}
