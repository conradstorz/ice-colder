# ice_contracts/board/ice_maker_monitor.py
"""
Shared contract models for the ice-maker monitor interface (v1.4.0).

These models are the machine-readable source of truth for the interface
between ice-colder (the VMC) and the external brand-specific monitor
project. JSON Schemas are generated from them into
docs/contracts/ice-maker-monitor/schemas/ by ice_contracts/generate.py.
Breaking changes require a major CONTRACT_VERSION bump.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ice_contracts.board.common import CHANNEL_ID_PATTERN, ChannelDescriptor, _utc_now
from ice_contracts.board.common import CommandAck as CommandAck
from ice_contracts.board.common import SubsystemCommand as MonitorCommand
from ice_contracts.board.vending_machine import SubsystemCapabilities

# Version history: docs/architecture-history.md (Contract version history).
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
# ice_contracts.board.common.SubsystemCommand and ice_contracts.board.common.CommandAck,
# imported and re-exported above under their original names so every
# existing `from ice_contracts.board.ice_maker_monitor import MonitorCommand` (and
# `CommandAck`) keeps working — they are the same class objects, not
# subclasses or copies. See ice_contracts/board/common.py for the shared subsystem
# command channel (§1.1 of the system-tests design) and its param registry.
