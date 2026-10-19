// The ledger task (core 0, priority 10): owns the EventLog and the NVS store. Every money event
// from the bus task is appended to flash first and only then handed to comms; on each connect
// edge the whole unacked log is replayed oldest first, ahead of any new event. It also persists
// the payout idempotency store and turns the bus task's state mailbox into payment/state.
#pragma once

#include <stddef.h>

#include "event_log.h"
#include "queues.h"

// Opens the flash log, mints this boot's boot_id and loads the payout store. Call once from
// app_main before start_bus / start_ledger. False when flash is unusable (the board still runs;
// events are then held until the store recovers, which is loud in the log).
bool ledger_init(const Queues& q, const char* machine_id);
void start_ledger();

// The log, for the bus task's self_test depth check (count() is atomic; nothing else is shared).
const mdbcore::EventLog& ledger_event_log();
// The payout store read from flash at boot (valid for the program's life), or nullptr when none.
const char* ledger_payouts_blob(size_t& len);

// Comms calls this on every MQTT connect edge, and whenever it needs the log replayed (an Outcome it
// could not publish or hold): bumps the replay epoch, then the ledger replays the whole unacked
// log, oldest first, before any new event.
void ledger_connected();
// Epoch of the latest replay request. Outcome items are stamped with the epoch the ledger had
// processed when it queued them; comms drops any whose stamp differs from this (stale: a replay
// has been requested since, and will resend them in log order).
uint32_t ledger_replay_epoch();

// Replay-log pressure with hysteresis (set at 192 pending, cleared at 128), or the store is unusable.
// The bus task feeds it to Master::set_log_pressure. Atomic; safe from any task.
bool ledger_log_pressure();
