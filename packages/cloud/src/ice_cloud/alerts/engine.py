"""Alert engine: diff a machine report's faults against the previous faults (pure)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ice_contracts.cloud.report import FaultView, MachineReport


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


def diff_reports(
    previous: Sequence[FaultView],
    report: MachineReport,
    *,
    now: datetime,
    fingerprint: str,
) -> list[AlertEvent]:
    """Events for faults that appeared or vanished; cleared first, then raised, each by key."""
    current = {fault.key: fault for fault in report.faults}
    before = {fault.key: fault for fault in previous}
    events: list[AlertEvent] = []
    for key in sorted(before.keys() - current.keys()):
        old = before[key]
        events.append(
            AlertEvent(
                fingerprint=fingerprint,
                kind="cleared",
                at=now,
                key=key,
                code=old.code,
                severity=old.severity,
                gate=old.gate,
                description=old.description,
                sku=old.sku,
                product=old.product,
            )
        )
    for key in sorted(current.keys() - before.keys()):
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
