"""`vending_faults()` -- fault injection without the simulator."""

import pytest

from ice_sims.vending.faults import InjectedFault, vending_faults
from ice_sims.vending.simulator import VendingMachineSimulator

VENDING_FAULTS = [
    ("auger_jam", "medium", 0.001, "warning"),
    ("water_valve_stuck_open", "short", 0.0012, "critical"),
    ("ice_bin_empty", "medium", 0.0006, "warning"),
    ("motor_stall", "medium", 0.0006, "warning"),
    ("no_water_flow", "short", 0.0008, "warning"),
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

    async def sleep(self, seconds: float) -> None:
        raise AssertionError("unexpected sleep")

    def spawn(self, coro):
        raise AssertionError("unexpected spawn")

    def active_faults(self) -> set[str]:
        return set()


def _by_name(name: str) -> InjectedFault:
    return next(f for f in vending_faults() if f.name == name)


def test_fault_names_and_parameters_equal_the_im600xl_set():
    assert [
        (f.name, f.category, f.probability, f.severity) for f in vending_faults()
    ] == VENDING_FAULTS


def test_simulator_registers_exactly_those_faults_in_order():
    sim = VendingMachineSimulator()
    assert [
        (f.name, f.category, f.probability, f.severity) for f in sim.fault_defs
    ] == (VENDING_FAULTS)
    assert [f.message for f in sim.fault_defs] == [f.message for f in vending_faults()]


@pytest.mark.asyncio
async def test_water_valve_stuck_open_drives_only_the_valve():
    board = RecordingBoard()
    await _by_name("water_valve_stuck_open").activate(board)
    await _by_name("water_valve_stuck_open").recover(board)
    assert board.set_hw_calls == [
        ("water_valve_solenoid", True),
        ("water_valve_solenoid", False),
    ]


def test_removed_faults_are_gone():
    names = {f.name for f in vending_faults()}
    assert not {"door_stuck_open", "bag_drop_solenoid_stuck"} & names


@pytest.mark.asyncio
async def test_ice_bin_empty_touches_no_bin_channel():
    board = RecordingBoard()
    await _by_name("ice_bin_empty").activate(board)
    await _by_name("ice_bin_empty").recover(board)
    assert board.set_hw_calls == []


@pytest.mark.asyncio
async def test_log_only_faults_touch_no_hardware():
    board = RecordingBoard()
    for name in (
        "auger_jam",
        "motor_stall",
        "no_water_flow",
        "low_flow",
        "ice_bin_empty",
    ):
        await _by_name(name).activate(board)
        await _by_name(name).recover(board)
    assert board.set_hw_calls == []
