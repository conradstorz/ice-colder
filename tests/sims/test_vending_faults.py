"""`vending_faults()` -- fault injection without the simulator."""

import pytest

from ice_sims.vending.faults import InjectedFault, vending_faults
from ice_sims.vending.simulator import VendingMachineSimulator

PRE_SPLIT_FAULTS = [
    ("auger_jam", "medium", 0.001, "warning"),
    ("bag_drop_solenoid_stuck", "medium", 0.0008, "warning"),
    ("water_valve_stuck_open", "short", 0.0012, "critical"),
    ("ice_bin_empty", "medium", 0.0006, "warning"),
    ("motor_stall", "medium", 0.0006, "warning"),
    ("no_water_flow", "short", 0.0008, "warning"),
    ("door_stuck_open", "medium", 0.0006, "critical"),
    ("flow_runaway", "short", 0.0006, "warning"),
    ("slow_flow", "short", 0.0006, "warning"),
    ("low_flow", "short", 0.0, "warning"),
]


class RecordingBoard:
    """A fake `BoardPort` that records every `set_hw` call."""

    subsystem_name = "vending"

    def __init__(self) -> None:
        self.set_hw_calls: list[tuple[str, bool]] = []

    async def set_hw(self, device: str, state: bool) -> None:
        self.set_hw_calls.append((device, state))

    async def publish(self, topic_suffix, payload) -> None:
        raise AssertionError("fault injection must not publish directly")

    async def publish_current(self, channel: str, amps: float) -> None:
        raise AssertionError("unexpected publish_current")

    async def sleep(self, seconds: float) -> None:
        raise AssertionError("unexpected sleep")

    def spawn(self, coro):
        raise AssertionError("unexpected spawn")

    def active_faults(self) -> set[str]:
        return set()


def _by_name(name: str) -> InjectedFault:
    return next(f for f in vending_faults() if f.name == name)


def test_fault_names_and_parameters_equal_pre_split_registration():
    assert [
        (f.name, f.category, f.probability, f.severity) for f in vending_faults()
    ] == PRE_SPLIT_FAULTS


def test_simulator_registers_exactly_those_faults_in_order():
    sim = VendingMachineSimulator()
    assert [
        (f.name, f.category, f.probability, f.severity) for f in sim.fault_defs
    ] == (PRE_SPLIT_FAULTS)
    assert [f.message for f in sim.fault_defs] == [f.message for f in vending_faults()]


@pytest.mark.asyncio
async def test_water_valve_stuck_open_activate_records_todays_set_hw_calls():
    board = RecordingBoard()
    await _by_name("water_valve_stuck_open").activate(board)
    assert board.set_hw_calls == [
        ("water_valve_solenoid", True),
        ("water_flow_sensor", True),
    ]


@pytest.mark.asyncio
async def test_water_valve_stuck_open_recover_records_todays_set_hw_calls():
    board = RecordingBoard()
    await _by_name("water_valve_stuck_open").recover(board)
    assert board.set_hw_calls == [
        ("water_valve_solenoid", False),
        ("water_flow_sensor", False),
    ]


@pytest.mark.asyncio
async def test_bag_drop_and_bin_empty_hardware_effects():
    board = RecordingBoard()
    await _by_name("bag_drop_solenoid_stuck").recover(board)
    await _by_name("ice_bin_empty").activate(board)
    await _by_name("ice_bin_empty").recover(board)
    assert board.set_hw_calls == [
        ("bag_full_sensor", False),
        ("bag_drop_solenoid", False),
        ("bin_half_full", False),
        ("bin_half_full", True),
    ]


@pytest.mark.asyncio
async def test_log_only_faults_touch_no_hardware():
    board = RecordingBoard()
    for name in ("auger_jam", "motor_stall", "no_water_flow", "low_flow"):
        await _by_name(name).activate(board)
        await _by_name(name).recover(board)
    assert board.set_hw_calls == []
