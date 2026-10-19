// Status LED on the DevKitC-1's WS2812 (GPIO 48), driven by a minimal RMT byte encoder.
// Patterns, highest priority first: white blink = replay log full; blue blink = no broker;
// red = any present device offline; green = accepting; amber = inhibited.
#pragma once

// Creates the RMT channel (the only allocation) and starts the 100 ms update task.
void status_led_start(int gpio);

// Called by the ledger task as it learns the board state.
void status_led_set_state(bool accepting, bool device_offline);
void status_led_set_log_full(bool full);
