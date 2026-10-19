// The bus task (core 1, priority 20): owns UART1, the Link and the Master. It never touches flash
// or the network. Commands arrive on q.cmd_q, first acks leave on q.ack_q, money events leave on
// q.evt_q (a full queue makes the driver hold the event, i.e. defers polling), and state
// snapshots go to the ledger through the 1-deep q.state_q mailbox.
#pragma once

#include "queues.h"

void start_bus(const Queues& q);
