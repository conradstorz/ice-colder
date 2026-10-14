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
from ice_contracts.cloud.report import (
    CLOUD_CONTRACT_VERSION,
    MachineReport,
    UiIdentity,
)
from ice_edge.views.health import (
    build_fault_views,
    build_health_page_summary,
    build_health_summary,
    build_kpi_summary,
    subsystem_rows,
    faults_with_age,
)
from ice_ui.bundle import ICE_UI_VERSION, ui_digest

if TYPE_CHECKING:
    from ice_edge.app.machine import Machine
    from ice_edge.sale.maintenance_lease import MaintenanceHold


async def build_machine_report(
    machine: Machine,
    *,
    config: ConfigModel,
    seq: int,
    boot_id: str,
    now: datetime,
    holder_name: Callable[[MaintenanceHold], str | None] | None = None,
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
        faults=build_fault_views(
            faults_with_age(machine.vmc, monitor, machine.pending_sale_for_recovery)
        ),
        health_page=build_health_page_summary(
            monitor.get_summary() if monitor else None, subsystem_rows(monitor)
        ),
    )
