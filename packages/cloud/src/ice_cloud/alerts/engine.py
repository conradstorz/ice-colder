"""Alert engine: diff a machine report's faults against the previous key set (pure)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ice_contracts.board.vending_machine import FaultCode
from ice_contracts.cloud.report import MachineReport

KIND_ORDER = ("cleared", "raised")


@dataclass(frozen=True)
class AlertEvent:
    fingerprint: str
    kind: Literal["raised", "cleared", "offline", "online"]
    at: datetime
    key: str | None
    code: str | None
    severity: str | None
    gate: Literal["safety", "fulfillment", "alert"] | None
    description: str | None
    sku: str | None
    product: str | None


def _parse_key(key: str) -> tuple[str | None, str | None]:
    """Best-effort (code, sku) from a fault key: ``CODE``, ``CODE:sku`` or a bare sku."""
    code, sep, sku = key.partition(":")
    if sep:
        return code, sku or None
    try:
        FaultCode(key)
    except ValueError:
        return None, key
    return key, None


def diff_reports(
    previous_keys: frozenset[str],
    report: MachineReport,
    *,
    now: datetime,
    fingerprint: str,
) -> list[AlertEvent]:
    """Events for faults that appeared or vanished; cleared first, then raised, each by key."""
    current = {fault.key: fault for fault in report.faults}
    events: list[AlertEvent] = []
    for key in sorted(previous_keys - current.keys()):
        code, sku = _parse_key(key)
        events.append(
            AlertEvent(
                fingerprint=fingerprint,
                kind="cleared",
                at=now,
                key=key,
                code=code,
                severity=None,
                gate=None,
                description=None,
                sku=sku,
                product=None,
            )
        )
    for key in sorted(current.keys() - previous_keys):
        fault = current[key]
        events.append(
            AlertEvent(
                fingerprint=fingerprint,
                kind="raised",
                at=now,
                key=key,
                code=fault.code,
                severity=fault.severity,
                gate=fault.gate,
                description=fault.description,
                sku=fault.sku,
                product=fault.product,
            )
        )
    return events
