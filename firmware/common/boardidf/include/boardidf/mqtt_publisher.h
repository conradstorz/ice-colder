#pragma once

#include <stddef.h>

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

// One inbound message, already reassembled; delivered on the MQTT client's own task.
using MqttMessageCb = void (*)(const char* topic, size_t topic_len, const char* data, size_t data_len);

// Starts esp-mqtt (QoS 1 everywhere) with the contract LWT on
// vmc/<id>/heartbeat/<subsystem>. Safe to call before the network is up; the client
// retries on its own. The callback runs on the client's task and must not block.
void mqtt_start(const BoardIdentity& id, const Settings& settings, MqttMessageCb on_message);
bool mqtt_connected();
// True once after each (re)connect, for the comms task to run its connect sequence.
bool mqtt_take_connected_edge();
// Non-blocking enqueue at QoS 1; false when disconnected or the outbox refused it.
bool mqtt_publish(const char* topic, const char* payload, size_t len, bool retain);
bool mqtt_subscribe(const char* topic);
