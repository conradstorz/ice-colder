"""Build the ``MachineReport`` the uplink uploads (cloud spec section 6).

A pure read of machine state: nothing here changes the machine, raises a
fault or writes an event row. Sits below ``ice_edge.app``/``ice_edge.web``,
so ``Machine`` appears only as a type.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ice_common.build_info import BUILD_INFO
from ice_common.config_model import ConfigModel
from ice_contracts.board.vending_machine import EXPECTED_SUBSYSTEMS, FaultCode
from ice_contracts.cloud.report import (
    CLOUD_CONTRACT_VERSION,
    MachineReport,
    UiIdentity,
)
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.views.health import (
    build_fault_views,
    build_health_page_summary,
    build_health_summary,
    build_kpi_summary,
    fault_gate,
)
from ice_ui.bundle import ICE_UI_VERSION, ui_digest

if TYPE_CHECKING:
    from ice_edge.app.machine import Machine


def _faults_with_age(machine: Machine) -> list[dict]:
    """active_faults() joined with the health monitor's ages, the gate and
    (PAY-104 only) the pending sale -- the same join the Faults level does."""
    faults = machine.vmc.active_faults()
    monitor = machine.health_monitor
    ages = (
        {f["key"]: f["since_seconds"] for f in monitor.get_summary()["active_faults"]}
        if monitor
        else {}
    )
    for f in faults:
        f["since_seconds"] = ages.get(f["key"])
        f["gate"] = fault_gate(f)
        f["pending_sale"] = (
            machine.pending_sale_for_recovery()
            if f["code"] == FaultCode.PAY_104.value
            else None
        )
    return faults


def _subsystem_rows(machine: Machine) -> dict[str, dict]:
    monitor = machine.health_monitor
    live = monitor.get_summary()["subsystems"] if monitor else {}
    return {
        name: live.get(name, HealthMonitor.empty_subsystem_row())
        for name in EXPECTED_SUBSYSTEMS
    }


async def build_machine_report(
    machine: Machine,
    *,
    config: ConfigModel,
    seq: int,
    boot_id: str,
    now: datetime,
    holder_name: Callable[[object], str | None] | None = None,
) -> MachineReport:
    """Snapshot *machine* as a ``MachineReport``.

    ``holder_name`` resolves a maintenance hold to a display name (the web
    layer's access store owns user names); without it the raw holder id is
    used. ``boot_id`` must be ``str(uuid.uuid4())``.
    """

    def maintenance_holder() -> str | None:
        hold = machine.maintenance_hold
        if hold is None:
            return None
        if holder_name is not None:
            return holder_name(hold)
        return hold.holder_user_id

    monitor = machine.health_monitor
    health = await build_health_summary(
        vmc=machine.vmc,
        event_recorder=machine.event_recorder,
        health_monitor=monitor,
        availability=machine.availability,
        maintenance_holder=maintenance_holder,
    )
    return MachineReport(
        contract=CLOUD_CONTRACT_VERSION,
        machine_id=config.machine_id,
        sent_at=now,
        boot_id=boot_id,
        seq=seq,
        report_interval_seconds=config.cloud.report_interval_seconds,
        edge_build=BUILD_INFO.commit_short,
        ui=UiIdentity(digest=ui_digest(), ice_ui_version=ICE_UI_VERSION),
        health=health,
        kpi=await build_kpi_summary(machine.event_recorder),
        faults=build_fault_views(_faults_with_age(machine)),
        health_page=build_health_page_summary(
            monitor.get_summary() if monitor else None, _subsystem_rows(machine)
        ),
    )
