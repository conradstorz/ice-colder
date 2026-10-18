"""`Dispenser` against a fake `BoardPort` -- profile execution without the
simulator class."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ice_contracts.board.common import SubsystemCommand
from ice_contracts.board.dispenser_schema import (
    Accessory,
    AgitateStep,
    BaggedIceProfile,
    IceFillTimed,
    ReleaseBySensor,
    ReleaseTimed,
    WaterFillProfile,
    WaterFillTimed,
)
from ice_contracts.board.mqtt_messages import DispenseCommand, DispenserStatus
from ice_sims.vending.dispense import Dispenser
from ice_sims.vending.simulator import VendingMachineSimulator
from tests.async_helpers import settle
from tests.sims.vending_profiles import IM600XL_PROFILES, dispense_command


class ListBoard:
    """A fake `BoardPort`: records set_hw/publish events in one ordered list,
    sleeps instantly (recording the durations) and records spawned tasks."""

    subsystem_name = "vending"

    def __init__(self, faults: set[str] | None = None, gate=None) -> None:
        self.events: list[tuple] = []
        self.sleeps: list[float] = []
        self.spawned: list[asyncio.Task] = []
        self._faults = faults or set()
        self._gate = gate

    async def set_hw(self, device: str, state: bool) -> None:
        self.events.append(("hw", device, state))

    async def publish(self, topic_suffix, payload) -> None:
        if isinstance(payload, DispenserStatus):
            self.events.append(("status", payload.state, payload.detail))
        else:
            self.events.append(("publish", topic_suffix))

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._gate is not None and seconds == 5.0:
            await self._gate.wait()

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.spawned.append(task)
        return task

    def active_faults(self) -> set[str]:
        return set(self._faults)


def _ice_profile() -> BaggedIceProfile:
    return BaggedIceProfile(
        mechanism="bagged_ice",
        product_sku="ICE-T",
        agitate=AgitateStep(
            motor_channel="agitator_motor",
            run_seconds=3.0,
            stall_current_amps="unmonitored",
            current_channel="unmonitored",
        ),
        fill=IceFillTimed(
            proof="timed",
            motor_channel="auger_motor",
            max_run_seconds=17.0,
            stall_current_amps="unmonitored",
            current_channel="unmonitored",
        ),
        release=ReleaseTimed(
            proof="timed", solenoid_channel="trap_door_catch", pulse_seconds=1.0
        ),
        accessories={
            "fan": Accessory(
                channel="bag_fan", on_during=["fill"], lead_seconds=0.5, lag_seconds=2.0
            ),
            "light": Accessory(
                channel="heater_power",
                on_during=["all"],
                lead_seconds=0.0,
                lag_seconds=1.0,
            ),
        },
    )


def _water_profile(lag_seconds: float = 5.0) -> WaterFillProfile:
    return WaterFillProfile(
        mechanism="water_fill",
        product_sku="WATER-T",
        fill=WaterFillTimed(
            proof="timed", valve_channel="water_valve_solenoid", max_fill_seconds=1.0
        ),
        accessories={
            "light": Accessory(
                channel="heater_power",
                on_during=["all"],
                lead_seconds=0.0,
                lag_seconds=lag_seconds,
            )
        },
    )


@pytest.mark.asyncio
async def test_execute_profile_runs_a_bagged_ice_profile_on_a_fake_board():
    board = ListBoard()
    cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=_ice_profile())

    await Dispenser().execute_profile(board, cmd, request_id="req-1")

    statuses = [e[1] for e in board.events if e[0] == "status"]
    assert statuses == ["agitate", "fill", "release", "complete"]
    hw = [e[1:] for e in board.events if e[0] == "hw"]
    assert hw[0] == ("heater_power", True)  # ["all"] accessory comes first
    assert hw.index(("bag_fan", True)) < hw.index(("auger_motor", True))
    for channel in ("agitator_motor", "auger_motor", "trap_door_catch", "bag_fan"):
        assert (channel, False) in hw
    assert 17.0 in board.sleeps


@pytest.mark.asyncio
async def test_dispenser_and_simulator_yield_the_same_event_sequence():
    cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=_ice_profile())

    board = ListBoard()
    await Dispenser().execute_profile(board, cmd, request_id="req-1")

    sim = VendingMachineSimulator()
    sim.sleep = AsyncMock()
    sim_events: list[tuple] = []

    async def capture_publish(client, topic, payload):
        if isinstance(payload, DispenserStatus):
            sim_events.append(("status", payload.state, payload.detail))
        elif topic.startswith("hardware/io/"):
            sim_events.append(("hw", payload.device, payload.state))

    sim.publish = capture_publish
    await sim.execute_profile(AsyncMock(), cmd, request_id="req-1")

    assert [e for e in board.events if e[0] in {"hw", "status"}] == sim_events


@pytest.mark.asyncio
async def test_second_run_is_not_switched_off_by_the_first_runs_lag():
    """C4: one `Dispenser`, two runs, each through its own board. Run 1's
    lag-off is gated; run 2 claims the accessory back and cancels it."""
    dispenser = Dispenser()
    gate = asyncio.Event()
    board1 = ListBoard(gate=gate)
    board2 = ListBoard(gate=gate)
    cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=_water_profile())

    task1 = asyncio.ensure_future(dispenser.execute_profile(board1, cmd))
    await settle()
    assert ("hw", "heater_power", True) in board1.events
    assert not task1.done()  # blocked in its lag-off wait
    assert "heater_power" in dispenser.pending_lag_off

    task2 = asyncio.ensure_future(dispenser.execute_profile(board2, cmd))
    await settle(6)

    assert task1.done()
    assert ("hw", "heater_power", False) not in board1.events
    assert not task2.done()

    gate.set()
    await asyncio.wait_for(task2, timeout=2.0)
    assert ("hw", "heater_power", False) in board2.events
    assert dispenser.pending_lag_off == {}


@pytest.mark.asyncio
async def test_motor_stall_fault_is_read_from_the_board():
    board = ListBoard(faults={"motor_stall"})
    cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=_ice_profile())

    await Dispenser().execute_profile(board, cmd)

    assert ("status", "error", "stall 10.0 A") in board.events


@pytest.mark.asyncio
async def test_dispense_handler_drives_its_one_dispenser_through_a_fresh_board():
    sim = VendingMachineSimulator()
    run = AsyncMock()
    sim.dispenser.execute_profile = run
    client = AsyncMock()
    sim.publish = AsyncMock()
    params = DispenseCommand(
        slot=1, mechanism="bagged_ice", profile=_ice_profile()
    ).model_dump(mode="json")

    for request_id in ("req-00000001", "req-00000002"):
        cmd = SubsystemCommand.model_construct(
            request_id=request_id,
            command="dispense",
            params=params,
            timestamp=datetime.now(timezone.utc),
        )
        await sim.handle_command(client, cmd)
    await settle()

    assert run.await_count == 2
    first, second = run.await_args_list
    board1, board2 = first.args[0], second.args[0]
    assert board1 is not board2  # a fresh adapter per call
    assert board1.subsystem_name == "vending"
    assert first.args[1].slot == 1
    assert first.kwargs["request_id"] == "req-00000001"
    assert second.kwargs["request_id"] == "req-00000002"


def _statuses(board: ListBoard) -> list[str]:
    return [e[1] for e in board.events if e[0] == "status"]


def _hw_events(board: ListBoard) -> list[tuple[str, bool]]:
    return [e[1:] for e in board.events if e[0] == "hw"]


@pytest.mark.asyncio
async def test_catch_release_holds_the_catch_through_the_fill_and_drops_the_bag():
    board = ListBoard()

    await Dispenser().execute_profile(board, dispense_command(0))

    hw = _hw_events(board)
    assert hw.index(("trap_door_catch", True)) < hw.index(("agitator_motor", True))
    assert hw.index(("bag_full_sensor", True)) < hw.index(("trap_door_catch", False))
    assert hw.count(("trap_door_catch", True)) == 1
    assert _statuses(board) == ["agitate", "fill", "release", "complete"]
    assert 2.0 in board.sleeps  # the profile's drop_seconds
    # the catch is de-energized before the terminal report goes out
    terminal = board.events.index(("status", "complete", None))
    assert board.events.index(("hw", "trap_door_catch", False)) < terminal


@pytest.mark.asyncio
async def test_auger_jam_still_leaves_the_catch_de_energized():
    board = ListBoard(faults={"auger_jam"})

    await Dispenser().execute_profile(board, dispense_command(0))

    assert _statuses(board)[-1] == "timeout"
    catch = [s for d, s in _hw_events(board) if d == "trap_door_catch"]
    assert catch == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["motor_stall", "ice_bin_empty"])
async def test_every_failure_path_de_energizes_the_catch(fault):
    board = ListBoard(faults={fault})

    await Dispenser().execute_profile(board, dispense_command(0))

    catch = [s for d, s in _hw_events(board) if d == "trap_door_catch"]
    assert catch[0] is True
    assert catch[-1] is False


@pytest.mark.asyncio
async def test_a_cancelled_catch_vend_de_energizes_the_catch():
    gate = asyncio.Event()
    board = ListBoard(gate=gate)
    profile = IM600XL_PROFILES[0].model_copy(deep=True)
    profile.agitate.run_seconds = 5.0  # the gated sleep
    cmd = DispenseCommand(slot=0, mechanism="bagged_ice", profile=profile)

    task = asyncio.ensure_future(Dispenser().execute_profile(board, cmd))
    await settle()
    assert ("hw", "trap_door_catch", True) in board.events
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("hw", "trap_door_catch", False) in board.events


@pytest.mark.asyncio
async def test_ice_bin_empty_runs_the_fill_to_its_max_time_and_reports_bin_empty():
    board = ListBoard(faults={"ice_bin_empty"})

    await Dispenser().execute_profile(board, dispense_command(0))

    assert _statuses(board) == ["agitate", "fill", "bin_empty"]
    assert 45.0 in board.sleeps  # the profile's fill max_run_seconds
    assert all(d != "bin_half_full" for d, _ in _hw_events(board))
    assert not any(e[0] == "publish" and "bin" in e[1] for e in board.events)


@pytest.mark.asyncio
async def test_sensor_release_variant_still_works_on_declared_channels():
    profile = _ice_profile().model_copy(deep=True)
    profile.release = ReleaseBySensor(
        proof="door_sensor",
        solenoid_channel="trap_door_catch",
        sensor_channel="service_door",
        pulse_seconds=1.0,
        open_timeout_seconds=3.0,
        close_timeout_seconds=5.0,
    )
    board = ListBoard()

    await Dispenser().execute_profile(
        board, DispenseCommand(slot=0, mechanism="bagged_ice", profile=profile)
    )

    assert _statuses(board)[-1] == "complete"
    assert ("service_door", True) in _hw_events(board)


@pytest.mark.asyncio
@pytest.mark.parametrize("slot, gallons", [(1, 3.785), (2, 18.927)])
async def test_water_vend_counts_fill_pulses_to_the_target(slot, gallons):
    board = ListBoard()
    readings: list[tuple[str, float]] = []

    async def publish(topic_suffix, payload):
        if isinstance(payload, DispenserStatus):
            board.events.append(("status", payload.state, payload.detail))
        else:
            board.events.append(("publish", topic_suffix))
            readings.append((topic_suffix, payload.value))

    board.publish = publish

    await Dispenser().execute_profile(board, dispense_command(slot))

    assert _statuses(board) == ["fill", "complete"]
    assert {t for t, _ in readings} == {"telemetry/vending/fill_pulses"}
    assert readings[-1][1] == pytest.approx(450.0 * gallons, rel=0.001)
    assert "sensors/water_flow" not in [e[1] for e in board.events if e[0] == "publish"]
    assert {d for d, _ in _hw_events(board)} == {"water_valve_solenoid"}


@pytest.mark.asyncio
async def test_profile_naming_an_undeclared_channel_errors_without_driving_anything():
    profile = _ice_profile().model_copy(deep=True)
    profile.release = ReleaseBySensor(
        proof="door_sensor",
        solenoid_channel="trap_door_catch",
        sensor_channel="door_sensor",
        pulse_seconds=1.0,
        open_timeout_seconds=3.0,
        close_timeout_seconds=5.0,
    )
    board = ListBoard()

    await Dispenser().execute_profile(
        board, DispenseCommand(slot=0, mechanism="bagged_ice", profile=profile)
    )

    assert _hw_events(board) == []
    assert board.sleeps == []
    statuses = [e for e in board.events if e[0] == "status"]
    assert [s[1] for s in statuses] == ["error"]
    assert "door_sensor" in statuses[0][2]


@pytest.mark.asyncio
async def test_profile_driving_a_declared_input_errors_without_driving_anything():
    profile = _ice_profile().model_copy(deep=True)
    profile.agitate.motor_channel = "bag_full_sensor"  # an input
    board = ListBoard()

    await Dispenser().execute_profile(
        board, DispenseCommand(slot=0, mechanism="bagged_ice", profile=profile)
    )

    assert _hw_events(board) == []
    statuses = [e for e in board.events if e[0] == "status"]
    assert [s[1] for s in statuses] == ["error"]
    assert "bag_full_sensor" in statuses[0][2]


@pytest.mark.asyncio
async def test_a_profile_sensing_a_current_channel_is_refused():
    profile = _ice_profile().model_copy(deep=True)
    profile.agitate.stall_current_amps = 5.0
    profile.agitate.current_channel = "agitator_current"
    board = ListBoard()

    await Dispenser().execute_profile(
        board, DispenseCommand(slot=0, mechanism="bagged_ice", profile=profile)
    )

    assert _hw_events(board) == []
    assert "agitator_current" in [e for e in board.events if e[0] == "status"][0][2]
