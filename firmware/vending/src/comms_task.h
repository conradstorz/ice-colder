#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

// Starts networking, MQTT and the comms task on core 0. Consumes evt_q (vendcore::Event) and
// ack_q (vendcore::Ack) and feeds cmd_q (CmdItem). Never blocks the sequencer.
void start_comms(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q, const BoardIdentity& id,
                const Settings& settings);
