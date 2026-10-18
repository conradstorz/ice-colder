#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "ports.h"
#include "settings.h"

// Publisher that pushes into a FreeRTOS queue of vendcore::Event; never blocks.
struct QueuePublisher : vendcore::Publisher {
    explicit QueuePublisher(QueueHandle_t q) : q_(q) {}
    bool publish(const vendcore::Event& e) override { return xQueueSend(q_, &e, 0) == pdTRUE; }
    QueueHandle_t q_;
};

// Starts the sequencer on core 1 (priority 20, 10 ms period, task watchdog subscribed).
// cmd_q carries CmdItem in; evt_q (vendcore::Event) and ack_q (vendcore::Ack) carry results out.
void start_sequencer(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q, const Settings& settings);
