#pragma once

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

namespace boardidf {

// The shared boot prefix, in this order: NVS init (erase and retry when full or a new version),
// settings_init, settings_load into `settings`, after_load() (if given), console_start.
// after_load runs once the settings are loaded and before the console accepts input; a board
// registers extra settings or installs its flash guard there.
void board_boot(const BoardIdentity& identity, Settings& settings, void (*after_load)() = nullptr);

}  // namespace boardidf
