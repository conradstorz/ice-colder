from datetime import UTC, datetime
from uuid import uuid4

from ice_common.config_model import ConfigModel
from ice_contracts.board.vending_machine import FaultCode
from ice_contracts.cloud.report import MachineReport
from ice_edge.faults.availability import Availability
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.uplink.report import build_machine_report
from ice_ui.bundle import ui_digest

NOW = datetime(2026, 10, 13, 14, 5, tzinfo=UTC)


async def test_report_validates_and_carries_fault_and_digest(machine_fake_time):
    machine, _ = machine_fake_time
    machine.set_health_monitor(HealthMonitor())
    machine.set_availability(Availability())
    machine.vmc.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    cfg = ConfigModel()
    cfg.cloud.report_interval_seconds = 30

    report = await build_machine_report(
        machine, config=cfg, seq=7, boot_id=str(uuid4()), now=NOW
    )

    MachineReport.model_validate(report.model_dump(mode="json"))
    assert report.seq == 7
    assert report.sent_at == NOW
    assert report.report_interval_seconds == 30
    assert report.machine_id == cfg.machine_id
    assert report.ui.digest == ui_digest()
    assert FaultCode.ICE_301.value in {f.code for f in report.faults}
    assert report.health_page.subsystems_total > 0


async def test_report_without_monitor_or_recorder_still_builds(machine_fake_time):
    machine, _ = machine_fake_time
    report = await build_machine_report(
        machine, config=ConfigModel(), seq=0, boot_id=str(uuid4()), now=NOW
    )
    assert report.health_page.vmc_build is None
    assert report.kpi.summary is None


async def test_building_a_report_changes_no_machine_state(machine_fake_time):
    machine, _ = machine_fake_time
    before = machine.vmc.active_faults()
    await build_machine_report(
        machine, config=ConfigModel(), seq=0, boot_id=str(uuid4()), now=NOW
    )
    assert machine.vmc.active_faults() == before
    assert machine.vmc.state == "idle"
