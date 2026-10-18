#pragma once

#include "boardidf/board_identity.h"
#include "boardidf/settings.h"

// USB-serial line console: set <key> <value>, show, erase, reboot.
// `settings` is the boot-time copy shown by `show`; changes apply after reboot.
void console_start(const BoardIdentity& id, const Settings& settings);
