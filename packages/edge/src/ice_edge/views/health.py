"""Edge view-model builders for health, faults and KPIs.

Moved, not rewritten, from ``ice_edge.web.context.health_snapshot`` and the
``/kpi``, ``/health`` and ``/health/faults`` routes. They return the
``ice_contracts.cloud.report`` models, which the dashboard templates read
unchanged (Jinja ``x.y`` works on dicts and models alike) and the uplink
uploads in a ``MachineReport``.

This module sits below ``ice_edge.web`` and ``ice_edge.uplink``: it must not
import them (``ice_edge.app`` / ``ice_edge.web`` appear here only as types).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ice_contracts.board.vending_machine import (
    EXPECTED_SUBSYSTEMS,
    PAYMENT_BLOCKING_FAULTS,
    FaultCode,
)
from ice_contracts.cloud.report import (
    FaultView,
    HealthPageSummary,
    HealthSummary,
    KpiAverage,
    KpiSummary,
    KpiWindow,
    PendingSaleView,
)
from ice_edge.monitoring.health_monitor import HealthMonitor

if TYPE_CHECKING:
    from ice_edge.faults.availability import Availability
    from ice_edge.ledger.event_recorder import EventRecorder
    from ice_edge.sale.vmc import VMC

# active_faults() reports fault codes as strings (FaultCode.value); compare
# against the contract's enum set once, here, rather than in fault_gate.
_PAYMENT_BLOCKING_CODES = {code.value for code in PAYMENT_BLOCKING_FAULTS}


def fault_gate(fault: dict) -> str:
    """Classify one active fault into "safety" / "fulfillment" / "alert".

    Executor resolution 1 (task-6-brief.md): the spec asks for "the gate
    class per fault", but no such mapping exists in the codebase --
    ice_edge/faults/availability.py's Gate enum classifies permissive *rows*, not
    faults, and ice_contracts/board/vending_machine.py's FAULT_TABLE carries
    severity/scope but no gate. This derives it once, here, and nowhere
    else (never inline in a template, never re-derived):

      * code in PAYMENT_BLOCKING_FAULTS -> "safety" (those six codes are
        exactly the ones that can inhibit payment -- see
        ice_edge/faults/availability.py's Gate docstring)
      * else scope == "product" -> "fulfillment" (blocks only that sale)
      * else -> "alert" (blocks nothing)

    A reversible part-2 stand-in: the spec never defined a fault-to-gate
    mapping: a future task may add a real one (e.g. a `gate` field on
    FaultSpec) and delete this function.
    """
    if fault["code"] in _PAYMENT_BLOCKING_CODES:
        return "safety"
    if fault["scope"] == "product":
        return "fulfillment"
    return "alert"


def faults_with_age(
    vmc: VMC | None,
    health_monitor: HealthMonitor | None,
    pending_sale: Callable[[], dict | None],
) -> list[dict]:
    """vmc.active_faults() joined with the health monitor's `since_seconds`,
    the derived `gate` and (PAY-104 only) `pending_sale()` -- the shared
    source for the Faults level and the cloud report. A fault carries no age
    when no monitor is wired; an empty list when no VMC is wired."""
    if not vmc:
        return []
    faults = vmc.active_faults()
    ages: dict[str, float | None] = {}
    if health_monitor:
        ages = {
            f["key"]: f["since_seconds"]
            for f in health_monitor.get_summary()["active_faults"]
        }
    for f in faults:
        f["since_seconds"] = ages.get(f["key"])
        f["gate"] = fault_gate(f)
        # Only the machine-scope PAY-104 fault ever carries a pending sale.
        f["pending_sale"] = (
            pending_sale() if f["code"] == FaultCode.PAY_104.value else None
        )
    return faults


def subsystem_rows(health_monitor: HealthMonitor | None) -> dict[str, dict]:
    """One row per EXPECTED_SUBSYSTEMS entry: the live row when the monitor
    has ever heard from that subsystem, else the neutral empty row (for every
    subsystem when no monitor is wired)."""
    live = health_monitor.get_summary()["subsystems"] if health_monitor else {}
    return {
        name: live.get(name, HealthMonitor.empty_subsystem_row())
        for name in EXPECTED_SUBSYSTEMS
    }


async def build_health_summary(
    *,
    vmc: VMC | None,
    event_recorder: EventRecorder | None,
    health_monitor: HealthMonitor | None,
    availability: Availability | None,
    maintenance_holder: Callable[[], str | None],
) -> HealthSummary:
    """The single health computation shared by /status's hero banner,
    /pill's bar indicator and the cloud report.

    Yields the fields `partials/status_fragment.html` receives: `vmc_missing`,
    `status`, `is_healthy`, `issues`, `active_faults`, `payment_enabled`,
    `payment_reasons`, `machine_stopped` and `maintenance`. `is_healthy` is
    the one predicate both the hero and the pill must agree on:
    `len(issues) == 0 and payment_enabled is not False`, computed BEFORE
    `maintenance` filters SVC-102's line out of `issues` below, so a
    standby lease still shows amber/red (SVC-102 is a PAYMENT_BLOCKING_FAULTS
    member; it still disables payment and still fails `is_healthy` -- only
    its own line's wording is replaced by the friendlier `maintenance`
    banner). `issues` accumulates, in order: one line per active fault, an
    "errors in last 24h" line when the event recorder has any, a
    stale-subsystems line, and an out-of-range-temperature line.

    `maintenance` is the lease holder's display name -- the value of
    `maintenance_holder()`, called only when SVC-102 is an active fault; the
    callable returns None when no `MaintenanceHold` exists (both conditions,
    not just the fault, because a fault whose hold has already released must
    not show a stale holder name).

    When no VMC is wired, `vmc_missing` is True and every other key is a
    neutral placeholder (`is_healthy` None, empty lists) -- callers must
    check `vmc_missing` first rather than trust `is_healthy`.
    """
    if not vmc:
        return HealthSummary(
            vmc_missing=True,
            status=None,
            is_healthy=None,
            issues=[],
            active_faults=[],
            payment_enabled=None,
            payment_reasons=[],
            machine_stopped=None,
            maintenance=None,
        )

    status = vmc.get_status()
    issues: list[str] = []
    active_faults = vmc.active_faults()
    for f in active_faults:
        target = f["product"] or "machine"
        issues.append(f"{f['code']} {f['description']} ({target})")
        f["since_seconds"] = None

    if event_recorder:
        summary_24h = await asyncio.to_thread(event_recorder.get_summary, 24)
        errors_24h = summary_24h["errors"]
        if errors_24h > 0:
            issues.append(
                f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h"
            )

    if health_monitor:
        health = health_monitor.get_summary()
        ages = {f["key"]: f["since_seconds"] for f in health["active_faults"]}
        for f in active_faults:
            f["since_seconds"] = ages.get(f["key"])
        stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
        if stale:
            issues.append(f"Stale subsystems: {', '.join(stale)}")
        out_of_range = [
            loc for loc, temp in health["temperatures"].items() if not temp["in_range"]
        ]
        if out_of_range:
            issues.append(f"Temp issues: {', '.join(out_of_range)}")

    payment_enabled = availability.payment_enabled if availability else None
    payment_reasons = availability.payment_blocking_reasons() if availability else []

    # A safety permissive (e.g. service_door_closed) can drive
    # payment_enabled false without ever adding to `issues` -- no fault is
    # raised, just a permissive row failing. Fold that into the health
    # predicate directly rather than reformatting payment_reasons (bare
    # permissive names, not the fault table's human descriptions) into
    # `issues`: the Payment panel in the template already renders those
    # reasons whenever payment_enabled is false. `None` (no Availability
    # attached) is unknown, not unhealthy, so it must not flip this.
    is_healthy = len(issues) == 0 and payment_enabled is not False

    # Fold SVC-102's line into a friendlier "who's holding it" banner
    # instead of the raw fault description -- but only after is_healthy
    # above has already counted it, so the pill's amber/red state is
    # unaffected.
    maintenance = None
    if any(f["code"] == FaultCode.SVC_102.value for f in active_faults):
        maintenance = maintenance_holder()
        if maintenance is not None:
            svc102_prefix = f"{FaultCode.SVC_102.value} "
            issues = [line for line in issues if not line.startswith(svc102_prefix)]

    return HealthSummary(
        vmc_missing=False,
        status=status,
        is_healthy=is_healthy,
        issues=issues,
        active_faults=active_faults,
        payment_enabled=payment_enabled,
        payment_reasons=payment_reasons,
        machine_stopped=(None if payment_enabled is None else not payment_enabled),
        maintenance=maintenance,
    )


def build_fault_views(active_faults: list[dict]) -> list[FaultView]:
    """Project `_faults_with_age`-shaped dicts (active_faults() joined with
    `since_seconds`, optional `gate` and optional `pending_sale`) to
    `FaultView`s. `gate` is derived by `fault_gate` when the dict carries
    none; the edge-only `dom_key` and the pending sale's `name`/`slot`/
    `saved_at` are dropped (cloud spec section 6.4)."""
    views = []
    for f in active_faults:
        pending = f.get("pending_sale")
        views.append(
            FaultView(
                key=f["key"],
                sku=f["sku"],
                product=f["product"],
                code=f["code"],
                severity=f["severity"],
                scope=f["scope"],
                description=f["description"],
                since_seconds=f.get("since_seconds"),
                gate=f["gate"] if "gate" in f else fault_gate(f),
                pending_sale=(
                    PendingSaleView(
                        sku=pending["sku"],
                        price=pending["price"],
                        methods=pending["methods"],
                    )
                    if pending is not None
                    else None
                ),
            )
        )
    return views


async def build_kpi_summary(event_recorder: EventRecorder | None) -> KpiSummary:
    """The 24 h KPI cards' data: `summary` and `average` are both None when
    no recorder is wired (four placeholder cards). Only the keys
    `partials/kpi_fragment.html` reads are kept."""
    if not event_recorder:
        return KpiSummary(summary=None, average=None)
    summary = await asyncio.to_thread(event_recorder.get_summary, 24)
    average = await asyncio.to_thread(event_recorder.get_historical_average, 24)
    return KpiSummary(
        summary=KpiWindow(
            money_in=summary["money_in"],
            products_out=summary["products_out"],
            vends_failed=summary["vends_failed"],
            errors=summary["errors"],
            uptime_pct=summary["uptime_pct"],
        ),
        average=KpiAverage(
            money_in=average["money_in"],
            products_out=average["products_out"],
            errors=average["errors"],
            uptime_pct=average["uptime_pct"],
        ),
    )


def build_health_page_summary(
    monitor_summary: Mapping[str, Any] | None,
    subsystems: Mapping[str, Mapping[str, Any]],
) -> HealthPageSummary:
    """What `health.html` reads beyond `HealthSummary`: MQTT/VMC state, the
    build identity strip and the Subsystems tile's counts.

    `monitor_summary` is `HealthMonitor.get_summary()` (None with no health
    monitor, which hides the identity strip); `subsystems` is one row per
    expected subsystem. A subsystem is OK when alive and not stale."""
    vmc = monitor_summary["vmc"] if monitor_summary else None
    return HealthPageSummary(
        mqtt_connected=monitor_summary["mqtt_connected"] if monitor_summary else None,
        vmc_state=monitor_summary["vmc_state"] if monitor_summary else None,
        vmc_build=(
            {
                "commit": vmc["commit"],
                "commit_short": vmc["commit_short"],
                "source": vmc["source"],
                "uptime_seconds": vmc["uptime_seconds"],
                "machine_id": vmc["machine_id"],
            }
            if vmc
            else None
        ),
        subsystems_ok=sum(
            1 for r in subsystems.values() if r["alive"] and not r["stale"]
        ),
        subsystems_total=len(subsystems),
    )
