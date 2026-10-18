#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "ports.h"

// Publisher that pushes into a FreeRTOS queue of vendcore::Event; never blocks.
// Telemetry-class events (input/output changes, temperature, fill pulses) are shed first: they are
// dropped once fewer than kEvtHeadroom slots remain, leaving room for Outcome, Step and ButtonPressed,
// which always attempt the send.
struct QueuePublisher : vendcore::Publisher {
    static constexpr UBaseType_t kEvtHeadroom = 8;
    explicit QueuePublisher(QueueHandle_t q) : q_(q) {}
    bool publish(const vendcore::Event& e) override {
        using vendcore::EventType;
        const bool sheddable = e.type == EventType::Telemetry || e.type == EventType::InputChanged ||
                               e.type == EventType::OutputChanged || e.type == EventType::Temperature;
        if (sheddable && uxQueueSpacesAvailable(q_) < kEvtHeadroom) return false;
        return xQueueSend(q_, &e, 0) == pdTRUE;
    }
    QueueHandle_t q_;
};

// Starts the sequencer on core 1 (priority 20, 10 ms period, task watchdog subscribed).
// cmd_q carries CmdItem in; evt_q (vendcore::Event) and ack_q (vendcore::Ack) carry results out.
void start_sequencer(QueueHandle_t cmd_q, QueueHandle_t evt_q, QueueHandle_t ack_q);
