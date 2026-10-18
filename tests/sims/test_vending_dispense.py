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
    ReleaseTimed,
    WaterFillProfile,
    WaterFillTimed,
)
from ice_contracts.board.mqtt_messages import DispenseCommand, DispenserStatus
from ice_sims.vending.dispense import Dispenser
from ice_sims.vending.simulator import VendingMachineSimulator
from tests.async_helpers import settle


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

    async def publish_current(self, channel: str, amps: float) -> None:
        self.events.append(("current", channel, amps))

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
            proof="timed", solenoid_channel="bag_drop_solenoid", pulse_seconds=1.0
        ),
        accessories={
            "fan": Accessory(
                channel="bag_fan", on_during=["fill"], lead_seconds=0.5, lag_seconds=2.0
            ),
            "light": Accessory(
                channel="vending_now_light",
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
                channel="vending_now_light",
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
    assert hw[0] == ("vending_now_light", True)  # ["all"] accessory comes first
    assert hw.index(("bag_fan", True)) < hw.index(("auger_motor", True))
    for channel in ("agitator_motor", "auger_motor", "bag_drop_solenoid", "bag_fan"):
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
    assert ("hw", "vending_now_light", True) in board1.events
    assert not task1.done()  # blocked in its lag-off wait
    assert "vending_now_light" in dispenser.pending_lag_off

    task2 = asyncio.ensure_future(dispenser.execute_profile(board2, cmd))
    await settle(6)

    assert task1.done()
    assert ("hw", "vending_now_light", False) not in board1.events
    assert not task2.done()

    gate.set()
    await asyncio.wait_for(task2, timeout=2.0)
    assert ("hw", "vending_now_light", False) in board2.events
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
