// Flash quiet window: keeps the ledger's NVS work off the MDB bus.
//
// Why. On the ESP32-S3 an SPI flash write or erase disables the instruction/data cache and parks the
// other CPU until it finishes (tens of ms for a sector erase, a few ms for a page write). The bus
// task runs on core 1 and the ledger (all NVS calls) on core 0, so an NVS write on core 0 can freeze
// the bus task in the middle of an MDB transaction. A late ACK makes the peripheral re-report its
// last deposit, which would count a coin twice.
//
// Mechanism (a handshake, not a lock). The ledger sets `wanted`; the bus task looks at it only
// BETWEEN transactions, at the end of a round (flash_window_checkpoint). When set it gives the
// `quiet` semaphore and blocks on `done` for at most kBusHoldMs. The ledger takes `quiet`, does its
// NVS calls (a bounded batch, kBudgetMs), then gives `done`. MDB peripherals tolerate a gap of this
// size between polls. The bus feeds the task watchdog around the wait. If the ledger does not
// answer within kAcquireMs (bus busy or absent) it simply tries again on a later pass; if it
// overruns, the bus resumes after kBusHoldMs and counts the overrun (BusStats.flash_window_timeouts).
//
// What must run inside a window: NvsLogStore::append / erase_first / sweep_step / save_payouts and
// the blob reads inside EventLog::acked. The boot-time sweep in NvsLogStore::begin() is the one
// exception, it runs before the bus task exists. CONFIG_SPI_FLASH_AUTO_SUSPEND is a second layer
// that lets a long erase be suspended for the other core; this window is the actual fix.
#pragma once

#include <stdint.h>

void flash_window_init();  // creates the two semaphores (static storage); call once before the tasks start

// Bus task, between transactions. True when no window was requested or it was released in time;
// false when the ledger overran kBusHoldMs and the bus resumed without it.
bool flash_window_checkpoint();

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
