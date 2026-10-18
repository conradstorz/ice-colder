// Shim: AckCache moved to firmware/common/boardcore.
#pragma once

#include "boardcore/ack_cache.h"

namespace vendcore {
using boardcore::Ack;
using boardcore::AckCache;
using boardcore::AckStatus;
using boardcore::Phase;
}  // namespace vendcore
