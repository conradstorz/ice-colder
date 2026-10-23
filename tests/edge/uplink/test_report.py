import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from ice_common.config_model import ConfigModel, PhysicalDetails, Product
from ice_edge.app.machine import Machine
from tests.fakes import FakeTaskRunner
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


async def test_report_carries_common_name_clipped_to_200(machine_fake_time):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    cfg.physical.common_name = "Lobby Ice"
    report = await build_machine_report(
        machine, config=cfg, seq=0, boot_id=str(uuid4()), now=NOW
    )
    assert report.common_name == "Lobby Ice"

    cfg.physical.common_name = "n" * 300
    report = await build_machine_report(
        machine, config=cfg, seq=1, boot_id=str(uuid4()), now=NOW
    )
    assert report.common_name == "n" * 200
    MachineReport.model_validate(report.model_dump(mode="json"))


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


async def test_overlong_product_name_is_clipped_not_fatal():
    cfg = ConfigModel(
        physical=PhysicalDetails(
            products=[
                # model_construct bypasses the Product validator, as an
                # in-memory edit would; views.health.clip is the backstop.
                Product.model_construct(
                    sku="S" * 250, name="N" * 250, price=1.0, slot=1
                )
            ]
        )
    )
    machine = Machine(config=cfg, tasks=FakeTaskRunner())
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_health_monitor(HealthMonitor())
    machine.set_availability(Availability())
    product = machine.vmc.products[0]
    machine.vmc.raise_fault(FaultCode.ICE_301, sku=product.sku)
    machine.vmc.start_interaction()
    machine.vmc.selected_product = product

    report = await build_machine_report(
        machine, config=cfg, seq=1, boot_id=str(uuid4()), now=NOW
    )

    assert report.health.status.selected_product == "N" * 200
    assert any(f.product == "N" * 200 for f in report.faults)
    assert all(f.sku == "S" * 200 for f in report.faults if f.sku)
    MachineReport.model_validate(report.model_dump(mode="json"))


def test_validated_overlong_product_name_is_clipped_by_the_model():
    assert len(Product(sku="S", name="N" * 250).name) == 120
