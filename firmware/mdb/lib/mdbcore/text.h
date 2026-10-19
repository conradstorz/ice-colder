// Text helpers shared by the three device drivers.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace mdbcore {

// Copies a space/NUL padded fixed-width MDB text field (model, serial), trimming the padding.
// Bytes outside printable ASCII become '?': they are not JSON-safe and not meaningful text.
inline void copy_field(char* dst, size_t cap, const uint8_t* src, size_t n) {
    size_t len = n < cap - 1 ? n : cap - 1;
    while (len > 0 && (src[len - 1] == ' ' || src[len - 1] == 0)) --len;
    for (size_t i = 0; i < len; ++i) dst[i] = (src[i] >= 0x20 && src[i] < 0x7F) ? static_cast<char>(src[i]) : '?';
    dst[len] = '\0';
}

}  // namespace mdbcore
