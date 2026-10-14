"""Health view logic shared by the machine and the cloud portal.

Re-exports the report models templates are written against, and holds the
pill's three-way decision with its severity ranking (moved from
``ice_edge/web/routes/home.py``) so the cloud never re-implements it.
"""

from __future__ import annotations

from collections.abc import Sequence

from ice_contracts.cloud.report import (
    ActiveFaultView,
    FaultBase,
    FaultView,
    HealthSummary,
    KpiSummary,
)

__all__ = [
    "SEVERITY_RANK",
    "ActiveFaultView",
    "FaultBase",
    "FaultView",
    "HealthSummary",
    "KpiSummary",
    "highest_priority_fault",
    "pill_state",
]

# Rank highest-first for the pill's "which fault do I name" choice:
# critical > lockout > vend_failed > product_unavailable > warning > info.
# Values are the string values of ice_contracts.board.vending_machine.Severity,
# kept as plain strings since fault views carry ``severity`` as that enum's
# ``.value``.
SEVERITY_RANK = {
    "critical": 0,
    "lockout": 1,
    "vend_failed": 2,
    "product_unavailable": 3,
    "warning": 4,
    "info": 5,
}


def highest_priority_fault[F: FaultBase](faults: Sequence[F]) -> F | None:
    """The fault the pill should name: highest severity first, ties broken
    by the sequence's own order (product faults before machine faults, as
    ``FaultService.active_faults()`` emits them). ``min()`` is stable, so
    among equal-ranked entries it returns the first one encountered."""
    if not faults:
        return None
    return min(faults, key=lambda f: SEVERITY_RANK.get(f.severity, len(SEVERITY_RANK)))


def pill_state(health: HealthSummary) -> tuple[str, str]:
    """The bar pill's ``(state, text)``: ``("neutral", "—")`` with no VMC,
    ``("ok", "OK")`` when healthy, otherwise ``("fault", <top fault's code>)``
    or ``("fault", "ISSUE")`` when no active fault explains it."""
    if health.vmc_missing:
        return "neutral", "—"
    if health.is_healthy:
        return "ok", "OK"
    top = highest_priority_fault(health.active_faults)
    return "fault", (top.code if top else "ISSUE")
