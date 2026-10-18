// Shim: OutboundRing moved to firmware/common/boardcore.
#pragma once

#include "boardcore/outbound_ring.h"

namespace vendcore {
using boardcore::OutboundRing;
using boardcore::Prio;
using boardcore::RingEntry;
}  // namespace vendcore
