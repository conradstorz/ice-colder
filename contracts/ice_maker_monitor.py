# contracts/ice_maker_monitor.py
"""
Shared contract models for the ice-maker monitor interface (v1.0.0).

These models are the machine-readable source of truth for the interface
between ice-colder (the VMC) and the external brand-specific monitor
project. JSON Schemas are generated from them into
docs/contracts/ice-maker-monitor/schemas/ by contracts/generate.py.
Breaking changes require a major CONTRACT_VERSION bump.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

CONTRACT_VERSION = "1.0.0"

_CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChannelDescriptor(BaseModel):
    """One telemetry channel the monitor declares in its capabilities."""

    channel_id: str = Field(
        ..., pattern=_CHANNEL_ID_PATTERN, description="Slug; also the topic segment"
    )
    kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]
    unit: str = Field("", description="Unit, e.g. 'C', 'A', '%'; empty for binary")
    description: str = Field("", description="Human-readable channel description")
    interval_seconds: float = Field(
        ..., gt=0, le=3600, description="Declared publish cadence"
    )


class MonitorCapabilities(BaseModel):
    """Retained self-description published on connect and on channel changes."""

    subsystem: Literal["ice_maker"] = "ice_maker"
    contract_version: str = Field(..., description="Contract semver, e.g. '1.0.0'")
    brand: str = Field(..., description="Ice maker brand the monitor targets")
    model: str = Field(..., description="Ice maker model")
    firmware: str = Field(..., description="Monitor software version")
    channels: list[ChannelDescriptor] = Field(default_factory=list)
    commands: list[str] = Field(
        default_factory=list, description="Contract commands this monitor supports"
    )
    timestamp: datetime = Field(default_factory=_utc_now)


class ChannelReading(BaseModel):
    """One reading on telemetry/ice_maker/<channel_id>."""

    channel_id: str = Field(..., pattern=_CHANNEL_ID_PATTERN)
    value: float = Field(..., description="Binary channels use 0.0/1.0")
    timestamp: datetime = Field(default_factory=_utc_now)


class MonitorCommand(BaseModel):
    """VMC -> monitor command on cmd/ice_maker."""

    request_id: str = Field(..., min_length=8, max_length=64)
    command: Literal["power_cycle", "force_report", "set_interval"]
    params: dict[str, float] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _check_params(self):
        if self.command == "power_cycle":
            dwell = self.params.get("dwell_seconds")
            if dwell is None or not (5 <= dwell <= 300):
                raise ValueError("power_cycle requires dwell_seconds in [5, 300]")
        elif self.command == "set_interval":
            interval = self.params.get("interval_seconds")
            if interval is None or not (1 <= interval <= 3600):
                raise ValueError("set_interval requires interval_seconds in [1, 3600]")
        return self


class CommandAck(BaseModel):
    """Monitor -> VMC acknowledgement on cmd/ice_maker/ack."""

    request_id: str = Field(..., description="Echoed from the command")
    command: str
    status: Literal["ok", "rejected", "failed", "unsupported"]
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utc_now)
