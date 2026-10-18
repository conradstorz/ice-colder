# ice_sims/vending/simulator.py
"""
Vending machine interface simulator.

Simulates the Kooler Ice IM600XL's vending board: customer buttons, the
ice path (agitator, auger, bag fan, trap-door catch, bag-full sensor), the
water path (valve solenoid, flow-meter pulse counter) and the service
inputs. The channel set is the one declarative table in `channels`.

Dispensing itself (plan: dispenser profiles, Task 5) is driven entirely by
the command channel: the VMC sends a `dispense` command on
`cmd/vending` carrying the slot's full, validated `DispenseCommand`
(slot, mechanism, profile), and `_handle_dispense` runs it via
`execute_profile`, which plays out the profile's own agitate/fill/release
(bagged ice) or fill (water) steps against the hardware. There is no
longer a separate, legacy dispense topic or a board-side notion of "ice
vs. water by product name" -- the profile says exactly which channels to
drive and how.

This module composes the pieces (channel table, dispenser, customers,
faults, HA entities), owns publishing and the `_hw` dict, and runs the
loops. It holds no sequencing or physics logic itself.

Run: uv run python -m ice_sims.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
"""

import asyncio
import random
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import aiomqtt
from loguru import logger
from pydantic import BaseModel, ValidationError

from ice_contracts.board.common import SubsystemCommand
from ice_contracts.board.vending_machine import VendingChannel
from ice_contracts.board.mqtt_messages import (
    DispenseCommand,
    HardwareIO,
    SensorReading,
)
from ice_sims.base import CommandOutcome, ESP32Simulator, FaultDef
from ice_sims.vending.channels import (
    CHANNELS,
    HARDWARE_DEVICES,
    SENSOR_PUBLISH_INTERVAL,
)
from ice_sims.vending.climate import Climate
from ice_sims.vending.customer import Customers
from ice_sims.vending.dispense import Dispenser
from ice_sims.vending.faults import InjectedFault, vending_faults
from ice_sims.vending.ha import ha_discovery_entities
from ice_sims.vending.ro_tank import RoTank


# Inputs whose standing state is republished every sensor interval.
_PERIODIC_INPUTS: tuple[str, ...] = (
    VendingChannel.water_pressure_ok.value,
    VendingChannel.sump_high.value,
    VendingChannel.service_door.value,
)


class _Board:
    """Adapter exposing the simulator to the behaviour modules as a
    `BoardPort`, bound to one MQTT client. Stateless: it only forwards."""

    def __init__(self, sim: "VendingMachineSimulator", client: aiomqtt.Client):
        self._sim = sim
        self._client = client
        self.subsystem_name = sim.subsystem_name

    async def set_hw(self, device: str, state: bool) -> None:
        await self._sim.set_hw(self._client, device, state)

    async def publish(self, topic_suffix: str, payload: BaseModel) -> None:
        await self._sim.publish(self._client, topic_suffix, payload)

    async def sleep(self, seconds: float) -> None:
        await self._sim.sleep(seconds)

    @property
    def ro_tank(self) -> RoTank:
        return self._sim.ro_tank

    @property
    def climate(self) -> Climate:
        return self._sim.climate

    async def draw_water(self, gallons: float) -> None:
        for channel, state in self._sim.ro_tank.draw(gallons).items():
            await self._sim.set_hw(self._client, channel, state)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        return self._sim._spawn_background(coro)

    def active_faults(self) -> set[str]:
        return self._sim.active_fault_names


def _fault_def(sim: "VendingMachineSimulator", fault: InjectedFault) -> FaultDef:
    return FaultDef(
        name=fault.name,
        category=fault.category,
        probability=fault.probability,
        on_activate=lambda client, f=fault: f.activate(_Board(sim, client)),
        on_recover=lambda client, f=fault: f.recover(_Board(sim, client)),
        message=fault.message,
        severity=fault.severity,
    )


class VendingMachineSimulator(ESP32Simulator):
    """Simulates the vending machine button panel and dispenser hardware."""

    IDLE_MIN = 30.0  # min seconds between customers
    IDLE_MAX = 90.0  # max seconds between customers
    SUPPORTED_COMMANDS = ["dispense", "water_valve", "payment/enable"]
    CHANNELS = CHANNELS
    BRAND = "ice-colder"
    MODEL = "vending-sim"

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        **kwargs,
    ):
        super().__init__(subsystem_name="vending", **kwargs)
        # The single seam every wait in `execute_profile` goes through, so a
        # test can patch one instance attribute (`sim.sleep`) instead of the
        # module-global `asyncio.sleep` (which other tests in this file
        # still patch directly for the pre-existing command-channel tests --
        # both seams coexist because this is a thin, real wrapper, not an
        # internal fake clock). Defaults to `asyncio.sleep`; a constructor
        # kwarg lets a caller supply its own from the start.
        # Bound here, at construction time: monkeypatching `asyncio.sleep`
        # afterward no longer affects `execute_profile`'s waits (it still
        # affects `_handle_water_valve`'s own bare `asyncio.sleep` call).
        self.sleep = sleep or asyncio.sleep
        self.register_command("dispense", self._handle_dispense)
        # True while a dispense profile runs; a second dispense is rejected
        # "busy", as the board firmware does (vending ESP32 design 7).
        self._dispensing = False
        self.register_command("water_valve", self._handle_water_valve)
        self._customers = Customers(idle_min=self.IDLE_MIN, idle_max=self.IDLE_MAX)
        # Hardware state
        self._hw: dict[str, bool] = dict(HARDWARE_DEVICES)
        # The one long-lived dispenser: it owns the cross-run accessory
        # lag-off state (Copilot review PR #32, finding C4); every call
        # gets a fresh, stateless `_Board`.
        self._dispenser = Dispenser()
        # The two pure physical models the sensor loop ticks; the water
        # vend and the fault injections reach them through `_Board`.
        self.ro_tank = RoTank.default()
        self.climate = Climate(ambient_c=22.0)

        for fault in vending_faults():
            self.register_fault(_fault_def(self, fault))

    @property
    def dispenser(self) -> Dispenser:
        return self._dispenser

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for vending machine hardware."""
        return ha_discovery_entities(self.CHANNELS)

    def _pick_button(self) -> str:
        return self._customers.pick_button()

    def _arrival_factor(self, hour: int | None = None) -> float:
        return self._customers.arrival_factor(hour=hour)

    def _compute_idle_time(self, hour: int | None = None) -> float:
        return self._customers.compute_idle_time(self.active_fault_names, hour=hour)

    async def set_hw(self, client: aiomqtt.Client, device: str, state: bool):
        """Update hardware state and publish to MQTT."""
        self._hw[device] = state
        await self.publish(
            client, f"hardware/io/{device}", HardwareIO(device=device, state=state)
        )

    async def _publish_sensors(self, client: aiomqtt.Client):
        """Each pass: tick the RO tank and climate models, publish their
        changes first, then re-publish the standing state (service inputs,
        RO outputs, heater) so a late subscriber learns it, and publish
        `ambient`. Nothing publishes `sensors/water_flow`."""
        while True:
            changes = {
                **self.ro_tank.tick(),
                **self.climate.tick(random.gauss(0, 0.3)),
            }
            standing = {
                **{c: self._hw[c] for c in _PERIODIC_INPUTS},
                **self.ro_tank.outputs(),
                **self.climate.outputs(),
            }
            for channel in (*_PERIODIC_INPUTS, *changes):
                await self.set_hw(client, channel, standing[channel])
            for channel, state in standing.items():
                if channel not in _PERIODIC_INPUTS and channel not in changes:
                    await self.set_hw(client, channel, state)
            await self.publish(
                client,
                "sensors/temp/ambient",
                SensorReading(
                    location="ambient", value=round(self.climate.ambient_c, 2)
                ),
            )
            await asyncio.sleep(SENSOR_PUBLISH_INTERVAL)

    async def _force_report_extra(self, client: aiomqtt.Client) -> None:
        """Contract force_report: republish capabilities and every reading now."""
        await self._publish_capabilities(client)
        for channel, state in self._hw.items():
            await self.set_hw(client, channel, state)
        await self.publish(
            client,
            "sensors/temp/ambient",
            SensorReading(location="ambient", value=round(self.climate.ambient_c, 2)),
        )

    async def execute_profile(
        self,
        client: aiomqtt.Client,
        cmd: DispenseCommand,
        request_id: str | None = None,
    ) -> None:
        """Run the slot's profile on this simulator's one `Dispenser`
        through a fresh `_Board` bound to *client*."""
        await self._dispenser.execute_profile(
            _Board(self, client), cmd, request_id=request_id
        )

    async def _handle_dispense(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        """Command-channel `dispense`: validate the slot's full profile and
        run it via `execute_profile`.

        Completion-table amendment (2026-09-29, carried over from the
        pre-profile design): the ack means "accepted", not "done" --
        `execute_profile` can legitimately run well past
        `ACK_TIMEOUT_SECONDS`. Acks `phase="accepted"` immediately and
        runs the real sequence in the background (`_spawn_background`);
        its own completion is the terminal `hardware/dispenser` report
        `execute_profile` already publishes, carrying this command's
        `request_id` so `ice_edge/hal/command_dispatcher.py` can correlate
        it -- there is no second ack for this command.

        `COMMAND_PARAM_VALIDATORS["dispense"]` (ice_contracts/board/common.py)
        already validates `cmd.params` as a `DispenseCommand` before a
        `SubsystemCommand` built through the real wire path
        (`command_loop`) can even exist, so a `ValidationError` here only
        happens for a command built around that check (as the duplicate-
        request-id and rejection tests in this file do, via
        `SubsystemCommand.model_construct`) -- handled the same way
        `_handle_water_valve`'s docstring describes for its own
        defense-in-depth check: ack "rejected" with the validation
        message, never raise.
        """
        try:
            dispense_cmd = DispenseCommand.model_validate(cmd.params)
        except ValidationError as exc:
            first_line = (
                str(exc).splitlines()[0] if str(exc) else "invalid dispense params"
            )
            return CommandOutcome(status="rejected", detail=first_line)

        if self._dispensing:
            return CommandOutcome(status="rejected", detail="busy")

        async def _run() -> None:
            self._dispensing = True
            try:
                await self.execute_profile(
                    client, dispense_cmd, request_id=cmd.request_id
                )
            finally:
                self._dispensing = False

        self._spawn_background(_run())
        return CommandOutcome(
            status="ok",
            result={"slot": dispense_cmd.slot, "mechanism": dispense_cmd.mechanism},
            phase="accepted",
        )

    async def _handle_water_valve(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        """Command-channel `water_valve`: open the valve for `seconds` (1-10).

        `SubsystemCommand`'s own model validator runs
        `COMMAND_PARAM_VALIDATORS["water_valve"]` at construction time, so a
        command reaching this handler through the real wire path
        (`command_loop` -> `SubsystemCommand.model_validate` ->
        `handle_command`) always has `seconds` in [1, 10] already. An
        out-of-range value never gets this far any more: `command_loop`
        (`ice_sims/base.py`) now acks it "rejected" itself, from the raw
        payload, before a `SubsystemCommand` instance — and therefore this
        handler — ever exists. This handler no longer re-checks the range;
        doing so would only re-validate something the loop has already
        guaranteed.

        Completion-table amendment (2026-09-29): acks `phase="accepted"`
        immediately (matching `_handle_dispense`/`_handle_power_cycle`);
        the valve actually opens/closes in the background. Completion is a
        SECOND, `phase="completed"` ack on this same `cmd/vending/ack`
        topic and `request_id` (`publish_completion_ack`) — chosen over a
        new topic/event because the ack channel and its idempotency cache
        already exist and already correlate by `request_id`; nothing new
        needs to be invented on the wire to carry it.
        """
        seconds = cmd.params["seconds"]

        async def _run() -> None:
            try:
                await self.set_hw(client, "water_valve_solenoid", True)
                await asyncio.sleep(seconds)
            finally:
                # Copilot review (PR 22): the shutdown belongs here, not
                # after the sleep, so a cancelled task (MQTT disconnect) or
                # the hardware update raising still closes the valve
                # instead of leaving it open indefinitely. Idempotent to
                # call even when the opening update never completed.
                await self.set_hw(client, "water_valve_solenoid", False)
            await self.publish_completion_ack(client, cmd, {"seconds": seconds})

        self._spawn_background(_run())
        return CommandOutcome(
            status="ok", result={"seconds": seconds}, phase="accepted"
        )

    async def _customer_loop(self, client: aiomqtt.Client):
        """Simulate customers pressing buttons (see `Customers.run`)."""
        await self._customers.run(_Board(self, client))

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the button press, sensor monitoring, and dispense simulation."""
        logger.info("[vending] Starting vending machine simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._customer_loop(client))
            tg.create_task(self._publish_sensors(client))
            tg.create_task(self.command_loop(client))
