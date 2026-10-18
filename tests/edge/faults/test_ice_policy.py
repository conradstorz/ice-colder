from ice_common.config_model import ConfigModel
from ice_contracts.board.vending_machine import FaultCode, VendingChannel
from ice_edge.faults.fault_service import FaultChange
from ice_edge.faults.ice_policy import IcePolicy
from ice_edge.faults.availability import Availability


class FakeTasks:
    def schedule(self, delay, cb, *, label=""):
        raise AssertionError("not expected")


class FakeFaults:
    def __init__(self):
        self.cleared = 0
        self.lockouts = {}

    def clear_ice101_lockouts(self, by="auto"):
        self.cleared += 1


def make_policy(faults):
    return IcePolicy(
        faults=faults,
        tasks=FakeTasks(),
        bin_instrumented=lambda: True,
        cooldown_seconds=lambda: 1800.0,
    )


def test_bin_half_full_true_clears_ice101():
    faults = FakeFaults()
    make_policy(faults).on_hardware_io(VendingChannel.bin_half_full, True)
    assert faults.cleared == 1


def test_bin_half_full_false_clears_nothing():
    faults = FakeFaults()
    make_policy(faults).on_hardware_io(VendingChannel.bin_half_full, False)
    assert faults.cleared == 0


def test_other_device_clears_nothing():
    faults = FakeFaults()
    make_policy(faults).on_hardware_io("door", True)
    assert faults.cleared == 0


class Rec:
    def __init__(self):
        self.rows = []

    def record(self, kind, **kw):
        self.rows.append((kind, kw))


def cleared_by(rec):
    return [kw["metadata"]["by"] for k, kw in rec.rows if k == "lockout_cleared"]


def uninstrumented():
    av = Availability()
    av.set_bin_instrumented(False)
    return av


def labels(runner):
    return [c.label for c in runner.scheduled if c.label == "ice_retry_cooldown"]


async def test_raise_arms_and_fire_clears_with_by_cooldown(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    rec = Rec()
    machine.set_event_recorder(rec)
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    assert labels(runner) == ["ice_retry_cooldown"]
    assert runner.scheduled[-1].delay == 1800
    runner.fire("ice_retry_cooldown")
    assert machine.faults.is_locked("A1") is None
    assert cleared_by(rec) == ["cooldown"]
    assert labels(runner) == []
    assert labels(runner) == []


async def test_raise_again_after_fire_rearms(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    runner.fire("ice_retry_cooldown")
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    assert labels(runner) == ["ice_retry_cooldown"]
    assert len(labels(runner)) == 1


async def test_raise_twice_leaves_one_pending(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    machine.faults.raise_fault(FaultCode.ICE_101, sku="B1")
    assert labels(runner) == ["ice_retry_cooldown"]


async def test_operator_clear_cancels_timer(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    machine.faults.clear_fault("A1")
    assert labels(runner) == []
    assert labels(runner) == []


async def test_clear_of_one_of_two_keeps_timer(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    machine.faults.raise_fault(FaultCode.ICE_101, sku="B1")
    machine.faults.clear_fault("A1")
    assert labels(runner) == ["ice_retry_cooldown"]


async def test_instrumented_bin_arms_nothing_and_sensor_clears_auto(machine_fake_time):
    machine, runner = machine_fake_time
    av = Availability()
    av.set_bin_instrumented(True)
    machine.set_availability(av)
    rec = Rec()
    machine.set_event_recorder(rec)
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    assert labels(runner) == []
    await machine.telemetry.handle_hardware_io(
        "hardware/io/x", {"device": VendingChannel.bin_half_full, "state": True}
    )
    assert machine.faults.is_locked("A1") is None
    assert cleared_by(rec) == ["auto"]


def test_non_ice_change_ignored():
    p = IcePolicy(
        faults=FakeFaults(),
        tasks=FakeTasks(),
        bin_instrumented=lambda: False,
        cooldown_seconds=lambda: 1.0,
    )
    p.on_fault_change(FaultChange("raised", FaultCode.ICE_301, "A1", None))
    assert p.pending is False


def test_cooldown_config_bounds():
    import pytest
    from pydantic import ValidationError

    assert ConfigModel().physical.ice_retry_cooldown_seconds == 1800
    for bad in (59, 86401):
        with pytest.raises(ValidationError):
            ConfigModel.model_validate(
                {"physical": {"ice_retry_cooldown_seconds": bad}}
            )


async def _send_vending_caps(machine, *channel_ids):
    from ice_contracts.board.common import ChannelDescriptor
    from ice_contracts.board.vending_machine import (
        CONTRACT_VERSION,
        SubsystemCapabilities,
    )

    caps = SubsystemCapabilities(
        subsystem="vending",
        contract_version=CONTRACT_VERSION,
        brand="B",
        model="M",
        firmware="0.1.0",
        channels=[
            ChannelDescriptor(
                channel_id=c, kind="binary", interval_seconds=10, direction="input"
            )
            for c in channel_ids
        ],
    )
    await machine.telemetry.handle_capabilities(
        "capabilities/vending", caps.model_dump(mode="json")
    )


async def test_no_bin_caps_after_raise_arms_and_fire_clears_by_cooldown(
    machine_fake_time,
):
    machine, runner = machine_fake_time
    machine.set_availability(Availability())  # default: assumed instrumented
    rec = Rec()
    machine.set_event_recorder(rec)
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    assert labels(runner) == []
    await _send_vending_caps(machine, "button_ice")
    assert labels(runner) == ["ice_retry_cooldown"]
    runner.fire("ice_retry_cooldown")
    assert machine.faults.is_locked("A1") is None
    assert cleared_by(rec) == ["cooldown"]


async def test_bin_caps_cancel_pending_timer(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(uninstrumented())
    machine.faults.raise_fault(FaultCode.ICE_101, sku="A1")
    assert labels(runner) == ["ice_retry_cooldown"]
    await _send_vending_caps(machine, VendingChannel.bin_half_full)
    assert labels(runner) == []
    assert machine.faults.is_locked("A1") is not None


async def test_no_bin_caps_without_ice101_arms_nothing(machine_fake_time):
    machine, runner = machine_fake_time
    machine.set_availability(Availability())
    await _send_vending_caps(machine, "button_ice")
    assert labels(runner) == []
