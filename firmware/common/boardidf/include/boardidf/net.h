#pragma once

#include <stddef.h>

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

// Brings up Wi-Fi STA (or Ethernet when id.ethernet_supported && settings.use_eth), reconnects
// on loss, and starts SNTP. Non-blocking: poll net_up().
void net_start(const BoardIdentity& id, const Settings& settings);
bool net_up();
// Dotted-quad of the active netif; empty string until an address is held.
const char* net_ip();
// "esp32-" + 12 lowercase hex digits of the factory MAC.
const char* net_hardware_id();
// ISO-8601 UTC ("2026-10-18T12:00:00Z") once SNTP has set the clock; false before.
bool net_iso_time(char* out, size_t cap);
