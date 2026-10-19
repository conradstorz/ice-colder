#pragma once

#include <stddef.h>

#include "esp_random.h"
#include "ports.h"

// Lowercase hex from the hardware RNG (boot_id, session ids). With Wi-Fi up esp_random() is a
// true random source; before that it is a PRNG seeded from hardware noise.
struct EspIds : mdbcore::IdSource {
    void fill_hex(char* out, size_t n_chars) override {
        static const char hex[] = "0123456789abcdef";
        uint32_t word = 0;
        for (size_t i = 0; i < n_chars; ++i) {
            if (i % 8 == 0) word = esp_random();
            out[i] = hex[word & 0xF];
            word >>= 4;
        }
    }
};
