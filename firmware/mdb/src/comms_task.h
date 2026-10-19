#pragma once

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"
#include "queues.h"

// Starts networking, MQTT and the comms task on core 0. On each connect it clears the outbound
// ring, tells the ledger to replay the log, publishes the retained capabilities and HA discovery,
// a heartbeat, subscribes to cmd/mdb and payment/event/ack, and asks the bus for a state report.
// It decodes commands onto cmd_q and drains the ack and publish queues into MQTT through the
// 64-entry outbound ring.
//
// Event acks never enter the command queue: the MQTT callback decodes payment/event/ack with a
// non-blocking hand parser and coalesces it into a 4-slot table of (boot_id -> max seq) under a
// short critical section; the comms loop drains that table into ack_in_q. Acks are cumulative per
// boot, so an ack storm (an old-boot backlog being re-acked during a replay) collapses to at most
// four pending values and can neither overflow rx_q (depth 8, commands only) nor block the
// MQTT task.
void start_comms(const Queues& q, const BoardIdentity& id, const Settings& settings);
