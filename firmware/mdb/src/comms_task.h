#pragma once

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

// Starts networking, MQTT and the comms task on core 0: on each connect it publishes the retained
// capabilities and a heartbeat, then a heartbeat every 10 s. Grows command/event plumbing later.
void start_comms(const BoardIdentity& id, const Settings& settings);
