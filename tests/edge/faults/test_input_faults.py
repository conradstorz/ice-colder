import asyncio

import pytest

from ice_common.config_model import ConfigModel, PhysicalDetails, Product
from ice_contracts.board.vending_machine import (
    INPUT_FAULTS,
    FaultCode,
    VendingChannel,
)
from ice_edge.app.machine import Machine
from ice_edge.faults.availability import Availability
from tests.fakes import FakeMqtt, FakeTaskRunner, drain_pending


def _machine() -> tuple[Machine, FakeTaskRunner]:
    runner = FakeTaskRunner()
    cfg = ConfigModel(
        physical=PhysicalDetails(
            products=[Product(sku="W", price=1.0, slot=2, kind="water")]
        )
    )
    return Machine(cfg, tasks=runner), runner


async def _io(machine: Machine, device, state: bool) -> None:
    name = getattr(device, "value", device)
    await machine.telemetry.handle_hardware_io(
        f"hardware/io/{name}", {"device": name, "state": state}
    )


def _alerts(mqtt: FakeMqtt) -> list:
    return [p for t, p, _ in mqtt.published if t == "alerts"]


@pytest.fixture
async def mach():
    m, runner = _machine()
    m.attach_to_loop(asyncio.get_running_loop())
    m.set_availability(Availability())
    return m, runner


@pytest.mark.parametrize("entry", INPUT_FAULTS, ids=lambda e: e.code.value)
async def test_input_raises_and_opposite_clears(mach, entry):
    m, _ = mach
    await _io(m, entry.channel, entry.faulted_when)
    assert m.faults.has(entry.code)
    assert m.faults.lockouts == {}
    await _io(m, entry.channel, not entry.faulted_when)
    assert not m.faults.has(entry.code)
    assert m.faults.lockouts == {}


@pytest.mark.parametrize("entry", INPUT_FAULTS, ids=lambda e: e.code.value)
async def test_only_sump_high_blocks_payment(mach, entry):
    m, _ = mach
    await _io(m, entry.channel, entry.faulted_when)
    expected = entry.code is not FaultCode.WTR_104
    assert m.availability.payment_enabled is expected


async def test_pressure_recovery_keeps_empty_tank_fault(mach):
    m, _ = mach
    await _io(m, VendingChannel.ro_tank_empty, True)
    await _io(m, VendingChannel.water_pressure_ok, False)
    await _io(m, VendingChannel.water_pressure_ok, True)
    assert m.faults.has(FaultCode.WTR_106)
    assert not m.faults.has(FaultCode.WTR_105)


async def test_existing_product_lockout_survives_pressure_cycle(mach):
    m, _ = mach
    m.faults.raise_fault(FaultCode.WTR_102, sku="W")
    await _io(m, VendingChannel.water_pressure_ok, False)
    await _io(m, VendingChannel.water_pressure_ok, True)
    assert m.faults.is_locked("W") is FaultCode.WTR_102


async def test_repeated_failing_state_alerts_once(mach):
    m, runner = mach
    mqtt = FakeMqtt()
    m.set_mqtt_client(mqtt)
    await _io(m, VendingChannel.sump_high, True)
    await _io(m, VendingChannel.sump_high, True)
    await drain_pending(runner)
    assert len(_alerts(mqtt)) == 1


async def test_ro_overrun_raises_and_pump_off_keeps_it(mach):
    m, runner = mach
    await _io(m, VendingChannel.ro_production, True)
    runner.fire("ro_overrun")
    assert m.faults.has(FaultCode.WTR_107)
    await _io(m, VendingChannel.ro_production, False)
    assert m.faults.has(FaultCode.WTR_107)
    await _io(m, VendingChannel.ro_tank_full, True)
    assert not m.faults.has(FaultCode.WTR_107)


async def test_ro_timer_uses_configured_limit(mach):
    m, runner = mach
    await _io(m, VendingChannel.ro_production, True)
    (call,) = [c for c in runner.scheduled if c.label == "ro_overrun"]
    assert call.delay == 3600.0


async def test_pump_off_before_timer_cancels_it(mach):
    m, runner = mach
    await _io(m, VendingChannel.ro_production, True)
    await _io(m, VendingChannel.ro_production, False)
    assert not [c for c in runner.scheduled if c.label == "ro_overrun"]
    assert not m.faults.has(FaultCode.WTR_107)


async def test_on_off_on_leaves_one_pending_timer(mach):
    m, runner = mach
    for state in (True, False, True):
        await _io(m, VendingChannel.ro_production, state)
    assert len([c for c in runner.scheduled if c.label == "ro_overrun"]) == 1


async def test_rearm_without_off_leaves_one_pending_timer(mach):
    m, runner = mach
    await _io(m, VendingChannel.ro_production, True)
    await _io(m, VendingChannel.ro_production, True)
    assert len([c for c in runner.scheduled if c.label == "ro_overrun"]) == 1


async def test_tank_full_without_overrun_records_nothing(mach):
    m, runner = mach
    mqtt = FakeMqtt()
    m.set_mqtt_client(mqtt)
    await _io(m, VendingChannel.ro_tank_full, True)
    await drain_pending(runner)
    assert _alerts(mqtt) == []
    assert not m.faults.has(FaultCode.WTR_107)


async def test_unknown_device_is_ignored(mach):
    m, runner = mach
    await _io(m, "door", True)
    assert m.faults.machine_faults == {}
    assert not runner.scheduled


async def test_hardware_io_message_reaches_input_faults_once(mach):
    m, runner = mach
    mqtt = FakeMqtt()
    m.set_mqtt_client(mqtt)
    await _io(m, VendingChannel.sump_high, True)
    await drain_pending(runner)
    assert len(_alerts(mqtt)) == 1
    assert m.faults.has(FaultCode.WTR_104)


async def test_input_faults_wired_once_per_machine(mach):
    """The live ro_overrun timer count is the real guard: a double
    subscription would arm two timers (one per consumer). The raise
    notification check is incidental, since a second raise of an already
    active code is skipped and cannot reveal a duplicate subscriber."""
    m, runner = mach
    changes = []
    m.faults.subscribe_fault_change(changes.append)
    await _io(m, VendingChannel.sump_high, True)
    await _io(m, VendingChannel.ro_production, True)
    raised = [c for c in changes if c.action == "raised"]
    assert [c.code for c in raised] == [FaultCode.WTR_104]
    assert len([c for c in runner.scheduled if c.label == "ro_overrun"]) == 1


async def test_pump_off_leaves_no_firable_overrun_timer(mach):
    m, runner = mach
    await _io(m, VendingChannel.ro_production, True)
    await _io(m, VendingChannel.ro_production, False)
    with pytest.raises(LookupError):
        runner.fire("ro_overrun")
    assert not m.faults.has(FaultCode.WTR_107)


async def test_wtr105_clears_on_recovery_and_never_blocks_payment(mach):
    m, _ = mach
    await _io(m, VendingChannel.water_pressure_ok, False)
    assert m.faults.has(FaultCode.WTR_105)
    assert m.availability.payment_blocking_reasons() == []
    assert m.availability.payment_enabled
    await _io(m, VendingChannel.water_pressure_ok, True)
    assert not m.faults.has(FaultCode.WTR_105)
    assert m.availability.payment_blocking_reasons() == []
