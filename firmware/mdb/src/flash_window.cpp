#include "flash_window.h"

#include <atomic>

#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace {
constexpr uint32_t kAcquireMs = 200;  // ledger: longest wait for the bus to reach a checkpoint
constexpr uint32_t kBusHoldMs = 400;  // bus: longest it parks for the ledger (see flash_window.h)
constexpr uint32_t kRaceMs = 50;      // ledger: grace for a bus that took the request as we gave up
constexpr int64_t kBudgetUs = 40000;  // ledger: a batch should be released within this

StaticSemaphore_t g_quiet_buf;
StaticSemaphore_t g_done_buf;
SemaphoreHandle_t g_quiet = nullptr;
SemaphoreHandle_t g_done = nullptr;
std::atomic<bool> g_wanted{false};  // ledger asks; the bus consumes it with an atomic exchange
std::atomic<bool> g_parked{false};  // the bus took a request and is waiting for `done`
std::atomic<uint32_t> g_max_hold_us{0};

FlashWindow g_blocking;  // console only: one operator command at a time
}  // namespace

void flash_window_init() {
    g_quiet = xSemaphoreCreateBinaryStatic(&g_quiet_buf);
    g_done = xSemaphoreCreateBinaryStatic(&g_done_buf);
}

bool flash_window_checkpoint() {
    // Taking the request is one atomic step: either the bus owns it (and parks) or the ledger
    // withdrew it first. There is no window in which both believe different things.
    if (!g_wanted.exchange(false)) return true;
    // A `done` left by an earlier overrun cannot release this window: the ledger gives `done` only
    // after taking `quiet`, which happens after this line.
    xSemaphoreTake(g_done, 0);
    g_parked.store(true);
    esp_task_wdt_reset();
    xSemaphoreGive(g_quiet);
    const bool released = xSemaphoreTake(g_done, pdMS_TO_TICKS(kBusHoldMs)) == pdTRUE;
    g_parked.store(false);
    esp_task_wdt_reset();
    return released;  // one overrun per window, however long the ledger kept going
}

bool FlashWindow::open() {
    if (open_) return true;
    xSemaphoreTake(g_quiet, 0);  // drop a stale token from an abandoned request
    g_wanted.store(true);
    bool got = xSemaphoreTake(g_quiet, pdMS_TO_TICKS(kAcquireMs)) == pdTRUE;
    if (!got && !g_wanted.exchange(false)) {
        // The bus consumed the request just as we gave up: it is parking for us, so wait for it.
        got = xSemaphoreTake(g_quiet, pdMS_TO_TICKS(kRaceMs)) == pdTRUE;
    }
    if (!got) return false;
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
    if (g_parked.load()) xSemaphoreGive(g_done);  // nothing to release after an overrun
}

uint32_t FlashWindow::max_hold_us() { return g_max_hold_us.load(); }

void flash_window_open_blocking() {
    for (int i = 0; i < 5 && !g_blocking.open(); ++i) {
    }
}

void flash_window_close_blocking() { g_blocking.close(); }
