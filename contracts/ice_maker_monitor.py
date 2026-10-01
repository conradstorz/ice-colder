# contracts/ice_maker_monitor.py
"""
Shared contract models for the ice-maker monitor interface (v1.4.0).

These models are the machine-readable source of truth for the interface
between ice-colder (the VMC) and the external brand-specific monitor
project. JSON Schemas are generated from them into
docs/contracts/ice-maker-monitor/schemas/ by contracts/generate.py.
Breaking changes require a major CONTRACT_VERSION bump.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from contracts.common import CHANNEL_ID_PATTERN, ChannelDescriptor, _utc_now
from contracts.common import CommandAck as CommandAck
from contracts.common import SubsystemCommand as MonitorCommand
from contracts.vending_machine import SubsystemCapabilities

# 1.1.0 -> 1.2.0: minor bump. The command/ack models (MonitorCommand,
# CommandAck) move to contracts/common.py as SubsystemCommand/CommandAck and
# are re-exported here under their original names — same classes, wire
# format unchanged except the ack's new optional `result` field. Additive,
# so today's ice-maker firmware and the VMC's current ack handler still
# validate.
#
# 1.2.0 -> 1.3.0 (2026-09-29): minor bump, additive. `CommandAck` gains
# `phase` ("accepted" | "completed", default "completed" -- a present-day
# ack with no `phase` key still validates unchanged). `power_cycle` now
# acks "accepted" as soon as it starts (unchanged: still within the 10 s
# ack deadline, still enforcing the 300 s lockout) and sends a SECOND,
# `phase="completed"` ack, same topic and `request_id`, once the dwell
# actually elapses -- see docs/contracts/ice-maker-monitor/CONTRACT.md's
# completion table. `set_interval`, `ping`, `self_test` and `force_report`
# are unaffected; their single ack is still both accept and completion.
#
# 1.3.0 -> 1.4.0 (2026-09-30): minor bump, additive. `ChannelDescriptor`
# (contracts/common.py) gains `direction` ("input" | "output", default
# "input") and `driven_by` (str | None, default None) so a board's
# capabilities document can say which channels it drives versus senses,
# and which command's refusal inhibits an output. Both fields are
# optional with defaults, so every present-day channel descriptor still
# validates unchanged.
CONTRACT_VERSION = "1.4.0"

_CHANNEL_ID_PATTERN = CHANNEL_ID_PATTERN  # kept for ChannelReading

__all__ = [
    "CONTRACT_VERSION",
    "ChannelDescriptor",
    "ChannelReading",
    "CommandAck",
    "MonitorCapabilities",
    "MonitorCommand",
]


class MonitorCapabilities(SubsystemCapabilities):
    """Retained self-description published on connect and on channel changes.

    The ice-maker contract fixes `subsystem` and requires brand/model; the
    optional `hardware_id`/`ip` are the 1.1.0 additions.
    """

    subsystem: Literal["ice_maker"] = "ice_maker"
    brand: str = Field(..., description="Ice maker brand the monitor targets")
    model: str = Field(..., description="Ice maker model")


class ChannelReading(BaseModel):
    """One reading on telemetry/ice_maker/<channel_id>."""

    channel_id: str = Field(..., pattern=_CHANNEL_ID_PATTERN)
    value: float = Field(..., description="Binary channels use 0.0/1.0")
    timestamp: datetime = Field(default_factory=_utc_now)


# MonitorCommand and CommandAck are no longer defined here: they are
# contracts.common.SubsystemCommand and contracts.common.CommandAck,
# imported and re-exported above under their original names so every
# existing `from contracts.ice_maker_monitor import MonitorCommand` (and
# `CommandAck`) keeps working — they are the same class objects, not
# subclasses or copies. See contracts/common.py for the shared subsystem
# command channel (§1.1 of the system-tests design) and its param registry.
