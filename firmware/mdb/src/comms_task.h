#pragma once

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"
#include "queues.h"

// Starts networking, MQTT and the comms task on core 0. On each connect it clears the outbound
// ring, tells the ledger to replay the log, publishes the retained capabilities and HA discovery,
// a heartbeat, subscribes to cmd/mdb and payment/event/ack, and asks the bus for a state report.
// It decodes commands onto cmd_q, forwards event acks to the ledger, and drains the ack and
// publish queues into MQTT through the 64-entry outbound ring.
void start_comms(const Queues& q, const BoardIdentity& id, const Settings& settings);
