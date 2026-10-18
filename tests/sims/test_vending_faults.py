"""`vending_faults()` -- fault injection without the simulator."""

import pytest

from ice_sims.vending.ro_tank import RoTank
from ice_sims.vending.climate import Climate
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
    ("ro_tank_empty", "long", 0.0003, "warning"),
    ("low_pressure", "medium", 0.0004, "warning"),
    ("sump_high", "medium", 0.0003, "warning"),
    ("service_door_open", "short", 0.0006, "warning"),
    ("ro_fill_stuck", "long", 0.0002, "warning"),
]


class RecordingBoard:
    """A fake `BoardPort` that records every `set_hw` call."""

    subsystem_name = "vending"

    def __init__(self) -> None:
        self.set_hw_calls: list[tuple[str, bool]] = []
        self.ro_tank = RoTank.default()
        self.climate = Climate(ambient_c=22.0)
        self.draws: list[float] = []

    async def set_hw(self, device: str, state: bool) -> None:
        self.set_hw_calls.append((device, state))

    async def draw_water(self, gallons: float) -> None:
        self.draws.append(gallons)
        self.ro_tank.draw(gallons)

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


@pytest.mark.asyncio
async def test_ro_tank_empty_drains_and_blocks_filling_then_recovers():
    board = RecordingBoard()
    fault = _by_name("ro_tank_empty")
    await fault.activate(board)
    assert board.ro_tank.level_gal == 0.0
    assert board.ro_tank.fill_blocked
    await fault.recover(board)
    assert not board.ro_tank.fill_blocked
    assert board.set_hw_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "channel", "active_state", "idle_state"),
    [
        ("low_pressure", "water_pressure_ok", False, True),
        ("sump_high", "sump_high", True, False),
        ("service_door_open", "service_door", True, False),
    ],
)
async def test_input_faults_flip_their_input_and_restore_it(
    name, channel, active_state, idle_state
):
    board = RecordingBoard()
    await _by_name(name).activate(board)
    await _by_name(name).recover(board)
    assert board.set_hw_calls == [(channel, active_state), (channel, idle_state)]


@pytest.mark.asyncio
async def test_ro_fill_stuck_sticks_the_level_and_recovery_resets_the_cutoff():
    board = RecordingBoard()
    board.ro_tank.max_run_ticks = 2
    fault = _by_name("ro_fill_stuck")
    await fault.activate(board)
    assert board.ro_tank.fill_stuck
    board.ro_tank.tick()
    board.ro_tank.tick()
    assert board.ro_tank.cutoff_latched
    await fault.recover(board)
    assert not board.ro_tank.fill_stuck
    assert not board.ro_tank.cutoff_latched
    assert board.set_hw_calls == []
