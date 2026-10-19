// Flash quiet window: keeps the ledger's NVS work off the MDB bus.
//
// Why. On the ESP32-S3 an SPI flash write or erase disables the instruction/data cache and parks the
// other CPU until it finishes (tens of ms for a sector erase, a few ms for a page write). The bus
// task runs on core 1 and the ledger (all NVS calls) on core 0, so an NVS write on core 0 can freeze
// the bus task in the middle of an MDB transaction. A late ACK makes the peripheral re-report its
// last deposit, which would count a coin twice.
//
// Mechanism (a handshake, not a lock). The ledger sets `wanted`; the bus task consumes it with an
// atomic exchange, only BETWEEN transactions, at the end of a round (flash_window_checkpoint).
// Having taken it, it gives the `quiet` semaphore and blocks on `done` for at most kBusHoldMs. The
// ledger takes `quiet`, does its NVS calls (a bounded batch, kBudgetUs), then gives `done` (only if
// the bus is still parked). The bus feeds the task watchdog around the wait. If the ledger does not
// answer within kAcquireMs (bus busy or absent) it withdraws the request (the same exchange, so no
// spurious park) and tries again on a later pass.
//
// Numbers. kBusHoldMs = 400 ms: well above the worst NOR sector erase plus an NVS page switch
// (tens to ~150 ms), far below the MDB peripherals' non-response time (seconds) and the 5 s task
// watchdog. A batch is kept to kBudgetUs = 40 ms of work, so a normal window is a few ms. If the
// ledger overruns 400 ms the bus resumes and counts ONE overrun for that window
// (BusStats.flash_window_timeouts); it never re-parks for the same window, so an overrunning
// ledger cannot make the bus freeze again.
//
// What must run inside a window: every NVS call made while the machine is live: NvsLogStore
// append / get (replay and ack reads) / erase_first / sweep_step / save_payouts, and the console's
// settings writes (settings_set_flash_guard). The one exception is the read-only boot-time scan in
// NvsLogStore::begin(), which runs before the bus task exists. Wi-Fi keeps its config in RAM
// (esp_wifi_set_storage), and comms (network init, PHY calibration) starts before the bus task.
// CONFIG_SPI_FLASH_AUTO_SUSPEND is a second layer that lets a long erase be suspended for the other
// core; this window is the actual fix.
#pragma once

#include <stdint.h>

void flash_window_init();  // creates the two semaphores (static storage); call once before the tasks start

// Bus task, between transactions. True when no window was requested or it was released in time;
// false when the ledger overran kBusHoldMs and the bus resumed without it.
bool flash_window_checkpoint();

// Blocking helpers for non-ledger callers (the console): wait up to ~1 s for a window, then run
// anyway (an operator action on a quiet machine).
void flash_window_open_blocking();
void flash_window_close_blocking();

// Ledger task. RAII: open() blocks until the bus is quiet (false: try again later), close() lets
// the bus go. budget() is true while the batch is still inside kBudgetMs.
class FlashWindow {
public:
    FlashWindow() = default;
    ~FlashWindow() { close(); }
    FlashWindow(const FlashWindow&) = delete;
    FlashWindow& operator=(const FlashWindow&) = delete;

    bool open();
    bool is_open() const { return open_; }
    bool budget() const;
    void close();

    // Longest window held so far, microseconds (for the periodic ledger log).
    static uint32_t max_hold_us();

private:
    bool open_ = false;
    int64_t t0_us_ = 0;
};
