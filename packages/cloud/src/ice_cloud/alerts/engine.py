"""Alert engine: diff a machine report's faults against the previous faults (pure)."""

from collections.abc import Sequence
from datetime import datetime

from ice_cloud.store.repository import AlertEventRow
from ice_contracts.cloud.report import FaultView, MachineReport


def diff_reports(
    previous: Sequence[FaultView],
    report: MachineReport,
    *,
    now: datetime,
    fingerprint: str,
) -> list[AlertEventRow]:
    """Events for faults that appeared or vanished; cleared first, then raised, each by key."""
    current = {fault.key: fault for fault in report.faults}
    before = {fault.key: fault for fault in previous}
    events: list[AlertEventRow] = []
    for key in sorted(before.keys() - current.keys()):
        events.append(
            AlertEventRow.from_fault(fingerprint, "cleared", now, before[key])
        )
    for key in sorted(current.keys() - before.keys()):
        events.append(
            AlertEventRow.from_fault(fingerprint, "raised", now, current[key])
        )
    return events
