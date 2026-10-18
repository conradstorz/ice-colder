#pragma once

#include <stdint.h>

#include "channels.h"

// Vending-only console setting "polarity": one 0/1 per binary input channel (1 = active_low),
// stored as a blob under NVS namespace "vend", key "polarity" (unchanged from the pre-lift firmware).
constexpr int kPolarityCount = vendcore::kBinaryInputCount;

// Registers the setting with the shared settings table; call before console_start.
void polarity_register();
// Board default (kc868-a8) overlaid with the stored value when one exists.
void polarity_load(uint8_t out[kPolarityCount]);
