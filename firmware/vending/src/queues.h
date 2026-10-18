// Queue item types shared by the sequencer task and the comms task.
// cmd_q: CmdItem (comms -> sequencer); evt_q: vendcore::Event and ack_q: vendcore::Ack
// (sequencer -> comms).
#pragma once

#include "ack_cache.h"
#include "command.h"
#include "ports.h"

struct CmdItem {
    enum Kind { Decoded, VmcSeen, VmcOnline, ForceReport } kind;
    vendcore::Decoded decoded;
    bool online;
};
