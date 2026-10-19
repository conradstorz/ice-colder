#include "flash_window.h"

#include <atomic>

#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace {
constexpr uint32_t kAcquireMs = 200;  // ledger: longest wait for the bus to reach a checkpoint
constexpr uint32_t kBusHoldMs = 60;   // bus: longest it parks for the ledger
constexpr int64_t kBudgetUs = 40000;  // ledger: a batch should be released within this

StaticSemaphore_t g_quiet_buf;
StaticSemaphore_t g_done_buf;
SemaphoreHandle_t g_quiet = nullptr;
SemaphoreHandle_t g_done = nullptr;
std::atomic<bool> g_wanted{false};
std::atomic<uint32_t> g_max_hold_us{0};
}  // namespace

void flash_window_init() {
    g_quiet = xSemaphoreCreateBinaryStatic(&g_quiet_buf);
    g_done = xSemaphoreCreateBinaryStatic(&g_done_buf);
}

bool flash_window_checkpoint() {
    if (!g_wanted.load()) return true;
    xSemaphoreTake(g_done, 0);  // a token left by an earlier overrun must not release this window
    esp_task_wdt_reset();
    xSemaphoreGive(g_quiet);
    const bool released = xSemaphoreTake(g_done, pdMS_TO_TICKS(kBusHoldMs)) == pdTRUE;
    esp_task_wdt_reset();
    return released;
}

bool FlashWindow::open() {
    if (open_) return true;
    xSemaphoreTake(g_quiet, 0);  // drop a stale token from an abandoned request
    g_wanted.store(true);
    if (xSemaphoreTake(g_quiet, pdMS_TO_TICKS(kAcquireMs)) != pdTRUE) {
        g_wanted.store(false);
        return false;
    }
    open_ = true;
    t0_us_ = esp_timer_get_time();
    return true;
}

bool FlashWindow::budget() const { return open_ && esp_timer_get_time() - t0_us_ < kBudgetUs; }

void FlashWindow::close() {
    if (!open_) return;
    open_ = false;
    const uint32_t held = static_cast<uint32_t>(esp_timer_get_time() - t0_us_);
    if (held > g_max_hold_us.load()) g_max_hold_us.store(held);
    g_wanted.store(false);
    xSemaphoreGive(g_done);
}

uint32_t FlashWindow::max_hold_us() { return g_max_hold_us.load(); }
