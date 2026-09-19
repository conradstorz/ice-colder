# contracts/common.py
"""Pieces shared by every contract module. Contracts import from here, never
from each other."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChannelDescriptor(BaseModel):
    """One telemetry channel the monitor declares in its capabilities."""

    channel_id: str = Field(
        ..., pattern=CHANNEL_ID_PATTERN, description="Slug; also the topic segment"
    )
    kind: Literal["temperature", "current", "voltage", "level", "binary", "counter"]
    unit: str = Field("", description="Unit, e.g. 'C', 'A', '%'; empty for binary")
    description: str = Field("", description="Human-readable channel description")
    interval_seconds: float = Field(
        ..., gt=0, le=3600, description="Declared publish cadence"
    )
