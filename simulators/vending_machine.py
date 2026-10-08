# simulators/vending_machine.py
"""
Vending machine interface simulator.

Simulates the physical vending hardware: product buttons, ice dispense
(auger motor, agitator motor, fan, bag full sensor, bag drop solenoid),
water dispense (water valve solenoid, water flow sensor), ice bin half-full
detector, cabinet temperature sensor, and cabinet heater relay.

Dispensing itself (plan: dispenser profiles, Task 5) is driven entirely by
the command channel: the VMC sends a `dispense` command on
`cmd/vending` carrying the slot's full, validated `DispenseCommand`
(slot, mechanism, profile), and `_handle_dispense` runs it via
`_execute_profile`, which plays out the profile's own agitate/fill/release
(bagged ice) or fill (water) steps against the hardware. There is no
longer a separate, legacy dispense topic or a board-side notion of "ice
vs. water by product name" -- the profile says exactly which channels to
drive and how.

Run: uv run python -m simulators.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
"""

import asyncio
import contextlib
import random
from datetime import datetime

import aiomqtt
from loguru import logger
from pydantic import ValidationError

from contracts.common import ChannelDescriptor, SubsystemCommand
from contracts.ice_maker_monitor import ChannelReading
from contracts.vending_machine import DispenserOutcome, DispenseStep
from simulators.base import CommandOutcome, ESP32Simulator, FaultDef
from services.dispenser_schema import (
    Accessory,
    IceFillBySensor,
    ReleaseBySensor,
    WaterFillByVolume,
)
from services.mqtt_messages import (
    ButtonPress,
    DispenseCommand,
    DispenserStatus,
    HardwareIO,
    SensorReading,
)


# All binary hardware devices and their default states
HARDWARE_DEVICES = {
    # Ice dispense
    "auger_motor": False,
    "agitator_motor": False,
    "fan": False,
    "bag_full_sensor": False,
    "bag_drop_solenoid": False,
    # Water dispense
    "water_valve_solenoid": False,
    "water_flow_sensor": False,
    # Ice bin
    "bin_half_full": True,  # assume bin starts with ice
    # Cabinet
    "heater_relay": False,
    # Dispense profile accessories and sensors (plan: dispenser profiles)
    "bag_fan": False,
    "vending_now_light": False,
    "door_sensor": False,  # True = door open
}

SENSOR_PUBLISH_INTERVAL = 10.0  # seconds between periodic sensor publishes

# Spec §4.2's vending channel table, copied exactly, declaration order
# matching the table top to bottom, plus the dispenser-profile channels
# (plan: dispenser profiles, Task 5) appended at the end.
_BINARY_CHANNEL_INTERVAL = 1.0

_VENDING_CHANNELS: list[ChannelDescriptor] = [
    ChannelDescriptor(
        channel_id="cabinet",
        kind="temperature",
        unit="C",
        description="Cabinet temperature",
        interval_seconds=SENSOR_PUBLISH_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="water_flow",
        kind="counter",
        unit="gal",
        description="Cumulative water dispensed",
        interval_seconds=SENSOR_PUBLISH_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="bag_full_sensor",
        kind="binary",
        description="Ice bag full sensor",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="water_flow_sensor",
        kind="binary",
        description="Water flow sensor",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="bin_half_full",
        kind="binary",
        description="Ice bin half-full detector",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="auger_motor",
        kind="binary",
        description="Ice auger motor",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="dispense",
    ),
    ChannelDescriptor(
        channel_id="agitator_motor",
        kind="binary",
        description="Ice agitator motor",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="dispense",
    ),
    ChannelDescriptor(
        channel_id="bag_drop_solenoid",
        kind="binary",
        description="Bag drop solenoid",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="dispense",
    ),
    ChannelDescriptor(
        channel_id="water_valve_solenoid",
        kind="binary",
        description="Water valve solenoid",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="water_valve",
    ),
    ChannelDescriptor(
        channel_id="fan",
        kind="binary",
        description="Cabinet fan (autonomous)",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
    ),
    ChannelDescriptor(
        channel_id="heater_relay",
        kind="binary",
        description="Cabinet heater relay (autonomous)",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
    ),
    # --- Dispenser-profile channels (plan: dispenser profiles, Task 5) ---
    ChannelDescriptor(
        channel_id="bag_fan",
        kind="binary",
        description="Ice-bag cooling fan accessory",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="dispense",
    ),
    ChannelDescriptor(
        channel_id="vending_now_light",
        kind="binary",
        description="Vending-in-progress indicator light",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
        direction="output",
        driven_by="dispense",
    ),
    ChannelDescriptor(
        channel_id="door_sensor",
        kind="binary",
        description="Release-gate door sensor",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="agitator_current",
        kind="current",
        unit="A",
        description="Agitator motor current draw",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
    ChannelDescriptor(
        channel_id="auger_current",
        kind="current",
        unit="A",
        description="Auger motor current draw",
        interval_seconds=_BINARY_CHANNEL_INTERVAL,
    ),
]

# Bagged ice runs all three steps in order; water only ever runs "fill".
_BAGGED_ICE_STEPS: tuple[str, ...] = (
    DispenseStep.agitate.value,
    DispenseStep.fill.value,
    DispenseStep.release.value,
)
_WATER_STEPS: tuple[str, ...] = (DispenseStep.fill.value,)

# A plausible steady-state current reading for a monitored, healthy motor.
_DEFAULT_CURRENT_AMPS = 2.0
# Fallback stall-current reading when `motor_stall` is injected on a motor
# whose profile says its current sense is "unmonitored" -- the fault is a
# simulator-level test injection, independent of whether this particular
# profile claims to monitor current, so there is always a plausible number
# to put in the terminal report's `detail`.
_DEFAULT_STALL_AMPS = 10.0


class VendingMachineSimulator(ESP32Simulator):
    """Simulates the vending machine button panel and dispenser hardware."""

    IDLE_MIN = 30.0  # min seconds between customers
    IDLE_MAX = 90.0  # max seconds between customers
    SUPPORTED_COMMANDS = ["dispense", "water_valve", "payment/enable"]
    CHANNELS = _VENDING_CHANNELS
    BRAND = "ice-colder"
    MODEL = "vending-sim"

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="vending", **kwargs)
        self.register_command("dispense", self._handle_dispense)
        self.register_command("water_valve", self._handle_water_valve)
        self.num_buttons = len(self.config.products)
        logger.info(f"[vending] {self.num_buttons} products")
        # Hardware state
        self._hw: dict[str, bool] = dict(HARDWARE_DEVICES)
        self._cabinet_temp: float = 22.0  # starting cabinet temperature °C
        self._water_flow_total: float = 0.0  # cumulative gallons

        # Register faults
        self.register_fault(
            FaultDef(
                name="auger_jam",
                category="medium",
                probability=0.001,
                on_activate=self._on_auger_jam_activate,
                on_recover=self._on_auger_jam_recover,
                message="Auger motor jammed — ice bag fill timed out",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="bag_drop_solenoid_stuck",
                category="medium",
                probability=0.0008,
                on_activate=self._on_bag_drop_solenoid_stuck_activate,
                on_recover=self._on_bag_drop_solenoid_stuck_recover,
                message="Bag drop solenoid stuck — bag not releasing",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="water_valve_stuck_open",
                category="short",
                probability=0.0012,
                on_activate=self._on_water_valve_stuck_open_activate,
                on_recover=self._on_water_valve_stuck_open_recover,
                message="Water valve stuck open — water flowing continuously",
                severity="critical",
            )
        )
        self.register_fault(
            FaultDef(
                name="ice_bin_empty",
                category="medium",
                probability=0.0006,
                on_activate=self._on_ice_bin_empty_activate,
                on_recover=self._on_ice_bin_empty_recover,
                message="Ice bin empty — refill required",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="motor_stall",
                category="medium",
                probability=0.0006,
                on_activate=self._on_motor_stall_activate,
                on_recover=self._on_motor_stall_recover,
                message="Dispense motor stalled",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="no_water_flow",
                category="short",
                probability=0.0008,
                on_activate=self._on_no_water_flow_activate,
                on_recover=self._on_no_water_flow_recover,
                message="No water flow detected after valve opened",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="door_stuck_open",
                category="medium",
                probability=0.0006,
                on_activate=self._on_door_stuck_open_activate,
                on_recover=self._on_door_stuck_open_recover,
                message="Release gate door stuck open",
                severity="critical",
            )
        )
        self.register_fault(
            FaultDef(
                name="flow_runaway",
                category="short",
                probability=0.0006,
                on_activate=self._on_flow_runaway_activate,
                on_recover=self._on_flow_runaway_recover,
                message="Water flow continued well past target volume",
                severity="warning",
            )
        )

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for vending machine hardware."""
        entities = []

        # Binary sensors for hardware I/O
        binary_devices = [
            ("auger_motor", "Auger Motor", "running"),
            ("agitator_motor", "Agitator Motor", "running"),
            ("fan", "Fan", "running"),
            ("bag_full_sensor", "Bag Full Sensor", "occupancy"),
            ("bag_drop_solenoid", "Bag Drop Solenoid", "running"),
            ("water_valve_solenoid", "Water Valve Solenoid", "running"),
            ("water_flow_sensor", "Water Flow Sensor", "running"),
            ("bin_half_full", "Ice Bin Half Full", "occupancy"),
            ("heater_relay", "Cabinet Heater", "running"),
        ]
        for device_id, display_name, device_class in binary_devices:
            entities.append(
                {
                    "component": "binary_sensor",
                    "object_id": device_id,
                    "name": f"Vending {display_name}",
                    "state_topic_suffix": f"hardware/io/{device_id}",
                    "value_template": "{{ 'ON' if value_json.state else 'OFF' }}",
                    "device_class": device_class,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                }
            )

        # Cabinet temperature sensor
        entities.append(
            {
                "component": "sensor",
                "object_id": "cabinet_temp",
                "name": "Vending Cabinet Temperature",
                "state_topic_suffix": "sensors/temp/cabinet",
                "value_template": "{{ value_json.value }}",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "expire_after": 30,
            }
        )

        # Water flow total sensor
        entities.append(
            {
                "component": "sensor",
                "object_id": "water_flow_total",
                "name": "Vending Water Flow Total",
                "state_topic_suffix": "sensors/water_flow",
                "value_template": "{{ value_json.value }}",
                "device_class": "water",
                "unit_of_measurement": "gal",
                "state_class": "total_increasing",
            }
        )

        # Uptime
        entities.append(
            {
                "component": "sensor",
                "object_id": "uptime",
                "name": "Vending Machine Uptime",
                "state_topic_suffix": "heartbeat/vending",
                "value_template": "{{ value_json.uptime_seconds }}",
                "device_class": "duration",
                "unit_of_measurement": "s",
                "state_class": "total_increasing",
            }
        )

        return entities

    def _pick_button(self) -> int:
        return random.randint(0, self.num_buttons - 1)

    async def _sleep(self, seconds: float) -> None:
        """The single seam every wait in `_execute_profile` goes through,
        so a test can patch one instance attribute (`sim._sleep`) instead
        of the module-global `asyncio.sleep` (which other tests in this
        file still patch directly for the pre-existing command-channel
        tests -- both seams coexist because this is a thin, real wrapper,
        not an internal fake clock)."""
        await asyncio.sleep(seconds)

    async def _set_hw(self, client: aiomqtt.Client, device: str, state: bool):
        """Update hardware state and publish to MQTT."""
        self._hw[device] = state
        await self.publish(
            client, f"hardware/io/{device}", HardwareIO(device=device, state=state)
        )

    async def _publish_current(
        self, client: aiomqtt.Client, channel: str, amps: float
    ) -> None:
        """Publish one current-sense reading on the generic telemetry path
        (matching simulators/ice_maker.py's own `telemetry/<subsystem>/
        <channel_id>` convention) -- there is no per-slot current topic of
        its own."""
        await self.publish(
            client,
            f"telemetry/{self.subsystem_name}/{channel}",
            ChannelReading(channel_id=channel, value=amps),
        )

    async def _publish_sensors(self, client: aiomqtt.Client):
        """Periodically publish cabinet temperature, water flow, and bin level."""
        while True:
            # Simulate cabinet temperature drift
            self._cabinet_temp += random.gauss(0, 0.3)
            # Heater kicks in below 5°C
            if self._cabinet_temp < 5.0 and not self._hw["heater_relay"]:
                await self._set_hw(client, "heater_relay", True)
            elif self._cabinet_temp > 10.0 and self._hw["heater_relay"]:
                await self._set_hw(client, "heater_relay", False)

            # If water valve stuck open, keep incrementing flow
            if self._hw.get("water_flow_sensor") and self._hw.get(
                "water_valve_solenoid"
            ):
                self._water_flow_total += 0.1 * SENSOR_PUBLISH_INTERVAL

            await self.publish(
                client,
                "sensors/temp/cabinet",
                SensorReading(location="cabinet", value=round(self._cabinet_temp, 2)),
            )
            await self.publish(
                client,
                "sensors/water_flow",
                SensorReading(
                    location="water_flow",
                    value=round(self._water_flow_total, 2),
                    unit="gal",
                ),
            )

            # Publish current bin level state
            await self._set_hw(client, "bin_half_full", self._hw["bin_half_full"])

            await asyncio.sleep(SENSOR_PUBLISH_INTERVAL)

    # --- Fault activate/recover methods ---

    async def _on_auger_jam_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: auger jam — bag fill will time out")

    async def _on_auger_jam_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: auger_jam")

    async def _on_bag_drop_solenoid_stuck_activate(
        self, client: aiomqtt.Client
    ) -> None:
        logger.warning("[vending] FAULT: bag drop solenoid stuck")

    async def _on_bag_drop_solenoid_stuck_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bag_full_sensor", False)
        await self._set_hw(client, "bag_drop_solenoid", False)
        logger.info("[vending] Fault cleared: bag_drop_solenoid_stuck")

    async def _on_water_valve_stuck_open_activate(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "water_valve_solenoid", True)
        await self._set_hw(client, "water_flow_sensor", True)
        logger.warning("[vending] FAULT: water valve stuck open — flow incrementing")

    async def _on_water_valve_stuck_open_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "water_valve_solenoid", False)
        await self._set_hw(client, "water_flow_sensor", False)
        logger.info("[vending] Fault cleared: water_valve_stuck_open")

    async def _on_ice_bin_empty_activate(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bin_half_full", False)
        logger.warning("[vending] FAULT: ice bin empty")

    async def _on_ice_bin_empty_recover(self, client: aiomqtt.Client) -> None:
        await self._set_hw(client, "bin_half_full", True)
        logger.info("[vending] Fault cleared: ice_bin_empty")

    async def _on_motor_stall_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: motor stall")

    async def _on_motor_stall_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: motor_stall")

    async def _on_no_water_flow_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: no water flow detected")

    async def _on_no_water_flow_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: no_water_flow")

    async def _on_door_stuck_open_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: release gate door stuck open")

    async def _on_door_stuck_open_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: door_stuck_open")

    async def _on_flow_runaway_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[vending] FAULT: water flow runaway past target volume")

    async def _on_flow_runaway_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[vending] Fault cleared: flow_runaway")

    def _arrival_factor(self, hour: int | None = None) -> float:
        """Return an idle-time multiplier based on time of day.

        Peak hours (11am-2pm, 5pm-8pm): factor 0.5 (customers arrive twice as fast).
        Overnight (2am-6am): factor 2.0 (customers arrive half as fast).
        Otherwise: factor 1.0.
        """
        if hour is None:
            hour = datetime.now().hour
        if 11 <= hour < 14 or 17 <= hour < 20:
            return 0.5
        if 2 <= hour < 6:
            return 2.0
        return 1.0

    def _compute_idle_time(self, hour: int | None = None) -> float:
        """Return idle wait time in seconds, accounting for faults and time-of-day."""
        fault_active = (
            "auger_jam" in self._active_fault_names
            or "ice_bin_empty" in self._active_fault_names
        )
        if fault_active:
            return random.uniform(5.0, 15.0)
        factor = self._arrival_factor(hour=hour)
        return random.uniform(self.IDLE_MIN, self.IDLE_MAX) * factor

    # --- Profile execution (plan: dispenser profiles, Task 5) ---------------

    @staticmethod
    def _accessory_span(
        accessory: Accessory, step_names: tuple[str, ...]
    ) -> tuple[str, str]:
        """The (first, last) step name this accessory is active for --
        `["all"]` spans the whole run; otherwise the earliest and latest
        of its own `on_during` entries in step order."""
        if accessory.on_during == ["all"]:
            return step_names[0], step_names[-1]
        present = [s for s in step_names if s in accessory.on_during]
        return present[0], present[-1]

    async def _execute_profile(
        self,
        client: aiomqtt.Client,
        cmd: DispenseCommand,
        request_id: str | None = None,
    ) -> None:
        """Run the slot's full dispense profile and publish every step and
        the terminal outcome on `hardware/dispenser`.

        Replaces `_dispense_slot`/`_run_ice_dispense`/`_run_water_dispense`
        now that the VMC sends the slot's whole validated profile on the
        command channel instead of a bare slot number on the old,
        now-deleted dedicated dispense topic.
        `request_id` is the triggering `SubsystemCommand`'s own id (passed
        separately from `cmd`, a `DispenseCommand`, which has no such
        field of its own) so `DispenserStatus.request_id` can echo it, the
        same role it played for `_run_ice_dispense`/`_run_water_dispense`.

        Accessory handling: each accessory turns on, blocking this
        sequence for its own `lead_seconds`, the moment this run first
        reaches a step in its `on_during` (or immediately, for `["all"]`)
        -- that keeps "fan on before fill starts" a deterministic
        ordering, rather than a race against a concurrent task. Turning an
        accessory back off after its last relevant step is a background
        task (its `lag_seconds` delay must not hold up the rest of the
        sequence); every such task is cancelled and every accessory
        channel this run turned on is forced off in the `finally` below,
        so a failed or short-circuited run never leaves a fan or light on.
        """
        profile = cmd.profile
        slot = cmd.slot
        active = self._active_fault_names

        step_names = (
            _BAGGED_ICE_STEPS if profile.mechanism == "bagged_ice" else _WATER_STEPS
        )
        spans = {
            name: self._accessory_span(accessory, step_names)
            for name, accessory in profile.accessories.items()
        }
        accessory_on: set[str] = set()
        accessory_tasks: list[asyncio.Task] = []
        driven_on: set[str] = set()

        async def _drive_on(channel: str) -> None:
            await self._set_hw(client, channel, True)
            driven_on.add(channel)

        async def _drive_off(channel: str) -> None:
            await self._set_hw(client, channel, False)
            driven_on.discard(channel)

        async def _turn_off_later(channel: str, lag_seconds: float) -> None:
            await self._sleep(lag_seconds)
            await self._set_hw(client, channel, False)
            accessory_on.discard(channel)

        async def _enter_step(step: str) -> None:
            for name, accessory in profile.accessories.items():
                first, _last = spans[name]
                if first == step and accessory.channel not in accessory_on:
                    await self._set_hw(client, accessory.channel, True)
                    accessory_on.add(accessory.channel)
                    await self._sleep(accessory.lead_seconds)

        def _exit_step(step: str) -> None:
            for name, accessory in profile.accessories.items():
                _first, last = spans[name]
                if last == step and accessory.channel in accessory_on:
                    task = asyncio.get_running_loop().create_task(
                        _turn_off_later(accessory.channel, accessory.lag_seconds)
                    )
                    accessory_tasks.append(task)

        async def _publish_step(step: str) -> None:
            await self.publish(
                client,
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=step, request_id=request_id),
            )

        outcome: DispenserOutcome
        detail: str | None

        try:
            if profile.mechanism == "bagged_ice":
                outcome, detail = await self._run_bagged_ice(
                    client,
                    profile,
                    active,
                    _enter_step,
                    _exit_step,
                    _drive_on,
                    _drive_off,
                    _publish_step,
                )
            else:
                outcome, detail = await self._run_water_fill(
                    client,
                    profile,
                    active,
                    _enter_step,
                    _exit_step,
                    _drive_on,
                    _drive_off,
                    _publish_step,
                )
        finally:
            # `water_valve_stuck_open` deliberately leaves the valve/flow
            # sensor energised (its own on_activate already did this,
            # independent of any particular run) -- every other output
            # this run drove gets turned off, including on a failed run.
            if "water_valve_stuck_open" not in active:
                for channel in list(driven_on):
                    await self._set_hw(client, channel, False)
            for task in accessory_tasks:
                task.cancel()
            for task in accessory_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for channel in list(accessory_on):
                await self._set_hw(client, channel, False)

        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(
                slot=slot, state=outcome.value, request_id=request_id, detail=detail
            ),
        )

    async def _run_bagged_ice(
        self,
        client: aiomqtt.Client,
        profile,
        active: set[str],
        enter_step,
        exit_step,
        drive_on,
        drive_off,
        publish_step,
    ) -> tuple[DispenserOutcome, str | None]:
        """agitate -> fill -> release. Returns (outcome, detail)."""
        if "ice_bin_empty" in active:
            # Checked before anything else is touched -- no accessory,
            # motor, or publish happens on this path (plan resolution 2).
            return DispenserOutcome.bin_empty, None

        # --- agitate ---
        await enter_step(DispenseStep.agitate.value)
        await publish_step(DispenseStep.agitate.value)
        agitate = profile.agitate
        await drive_on(agitate.motor_channel)
        if agitate.current_channel != "unmonitored":
            await self._publish_current(
                client, agitate.current_channel, _DEFAULT_CURRENT_AMPS
            )
        if "motor_stall" in active:
            amps = (
                agitate.stall_current_amps + 1.0
                if agitate.stall_current_amps != "unmonitored"
                else _DEFAULT_STALL_AMPS
            )
            await drive_off(agitate.motor_channel)
            exit_step(DispenseStep.agitate.value)
            return DispenserOutcome.error, f"stall {amps:.1f} A"
        await self._sleep(agitate.run_seconds)
        await drive_off(agitate.motor_channel)
        exit_step(DispenseStep.agitate.value)

        # --- fill ---
        await enter_step(DispenseStep.fill.value)
        await publish_step(DispenseStep.fill.value)
        fill = profile.fill
        await drive_on(fill.motor_channel)
        if fill.current_channel != "unmonitored":
            await self._publish_current(
                client, fill.current_channel, _DEFAULT_CURRENT_AMPS
            )

        if isinstance(fill, IceFillBySensor):
            if "auger_jam" in active:
                await self._sleep(fill.max_run_seconds)
                await drive_off(fill.motor_channel)
                exit_step(DispenseStep.fill.value)
                return DispenserOutcome.timeout, None
            duration = min(6.0, fill.max_run_seconds / 2)
            await self._sleep(duration)
            await self._set_hw(client, fill.sensor_channel, True)
        else:  # IceFillTimed
            await self._sleep(fill.max_run_seconds)
        await drive_off(fill.motor_channel)
        exit_step(DispenseStep.fill.value)

        # --- release ---
        await enter_step(DispenseStep.release.value)
        await publish_step(DispenseStep.release.value)
        release = profile.release
        await drive_on(release.solenoid_channel)
        await self._sleep(release.pulse_seconds)
        await drive_off(release.solenoid_channel)

        if isinstance(release, ReleaseBySensor):
            if "bag_drop_solenoid_stuck" in active:
                exit_step(DispenseStep.release.value)
                return DispenserOutcome.jam, None
            if "door_stuck_open" in active:
                await self._set_hw(client, release.sensor_channel, True)
                await self._sleep(release.close_timeout_seconds)
                exit_step(DispenseStep.release.value)
                return DispenserOutcome.door_open, None
            await self._set_hw(client, release.sensor_channel, True)
            await self._sleep(1.0)
            await self._set_hw(client, release.sensor_channel, False)
            if isinstance(fill, IceFillBySensor):
                await self._set_hw(client, fill.sensor_channel, False)

        exit_step(DispenseStep.release.value)
        return DispenserOutcome.complete, None

    async def _run_water_fill(
        self,
        client: aiomqtt.Client,
        profile,
        active: set[str],
        enter_step,
        exit_step,
        drive_on,
        drive_off,
        publish_step,
    ) -> tuple[DispenserOutcome, str | None]:
        """fill only. Returns (outcome, detail)."""
        await enter_step(DispenseStep.fill.value)
        await publish_step(DispenseStep.fill.value)
        fill = profile.fill
        await drive_on(fill.valve_channel)

        if isinstance(fill, WaterFillByVolume):
            await self._set_hw(client, fill.flow_sensor_channel, True)
            if "no_water_flow" in active:
                await self._set_hw(client, fill.flow_sensor_channel, False)
                await self._sleep(fill.no_flow_grace_seconds)
                await drive_off(fill.valve_channel)
                exit_step(DispenseStep.fill.value)
                return DispenserOutcome.no_flow, None
            if "flow_runaway" in active:
                await self._sleep(
                    min(fill.max_fill_seconds, fill.no_flow_grace_seconds + 2.0)
                )
                await self._set_hw(client, fill.flow_sensor_channel, False)
                await drive_off(fill.valve_channel)
                exit_step(DispenseStep.fill.value)
                return DispenserOutcome.over_dispense, None
            duration = min(8.0, fill.max_fill_seconds / 2)
            await self._sleep(duration)
        else:  # WaterFillTimed
            await self._sleep(fill.max_fill_seconds)

        if "water_valve_stuck_open" in active:
            # Valve/flow sensor stay energised (see _execute_profile's
            # finally) -- matches the fault's existing effect.
            exit_step(DispenseStep.fill.value)
            return DispenserOutcome.complete, None

        if isinstance(fill, WaterFillByVolume):
            await self._set_hw(client, fill.flow_sensor_channel, False)
        await drive_off(fill.valve_channel)
        exit_step(DispenseStep.fill.value)
        return DispenserOutcome.complete, None

    async def _handle_dispense(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        """Command-channel `dispense`: validate the slot's full profile and
        run it via `_execute_profile`.

        Completion-table amendment (2026-09-29, carried over from the
        pre-profile design): the ack means "accepted", not "done" --
        `_execute_profile` can legitimately run well past
        `ACK_TIMEOUT_SECONDS`. Acks `phase="accepted"` immediately and
        runs the real sequence in the background (`_spawn_background`);
        its own completion is the terminal `hardware/dispenser` report
        `_execute_profile` already publishes, carrying this command's
        `request_id` so `services/command_dispatcher.py` can correlate
        it -- there is no second ack for this command.

        `COMMAND_PARAM_VALIDATORS["dispense"]` (contracts/common.py)
        already validates `cmd.params` as a `DispenseCommand` before a
        `SubsystemCommand` built through the real wire path
        (`_command_loop`) can even exist, so a `ValidationError` here only
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

        self._spawn_background(
            self._execute_profile(client, dispense_cmd, request_id=cmd.request_id)
        )
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
        (`_command_loop` -> `SubsystemCommand.model_validate` ->
        `_handle_command`) always has `seconds` in [1, 10] already. An
        out-of-range value never gets this far any more: `_command_loop`
        (`simulators/base.py`) now acks it "rejected" itself, from the raw
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
                await self._set_hw(client, "water_valve_solenoid", True)
                await self._set_hw(client, "water_flow_sensor", True)
                await asyncio.sleep(seconds)
            finally:
                # Copilot review (PR 22): the shutdown belongs here, not
                # after the sleep, so a cancelled task (MQTT disconnect) or
                # either hardware update raising still closes the valve and
                # clears the flow sensor instead of leaving them enabled
                # indefinitely. Idempotent to call even when the opening
                # updates never completed (or never ran at all).
                await self._set_hw(client, "water_valve_solenoid", False)
                await self._set_hw(client, "water_flow_sensor", False)
            await self.publish_completion_ack(client, cmd, {"seconds": seconds})

        self._spawn_background(_run())
        return CommandOutcome(
            status="ok", result={"seconds": seconds}, phase="accepted"
        )

    async def _customer_loop(self, client: aiomqtt.Client):
        """Simulate customers pressing buttons.

        Production dispensing now arrives entirely through the command
        channel (`cmd/vending`, `_handle_command` -> `_handle_dispense` ->
        `_execute_profile`), running concurrently via `_command_loop` --
        this loop no longer waits for, or itself runs, a dispense. It only
        generates the button-press traffic a real customer would (plus the
        occasional change-of-mind second press); the old "wait on the
        legacy dispense topic, then vend, maybe vend again" tail -- the
        impatient-customer timeout and the repeat-customer purchase --
        depended entirely on that now-deleted topic/queue and is gone
        with it.
        """
        warned_no_products = False
        while True:
            if self.num_buttons == 0:
                if not warned_no_products:
                    logger.warning(
                        "[vending] No products configured — no buttons to press"
                    )
                    warned_no_products = True
                self.config = self.load_config(self._config_path)
                if self.config.products:
                    self.num_buttons = len(self.config.products)
                    logger.info(
                        f"[vending] Products loaded: {self.num_buttons} products"
                    )
                await asyncio.sleep(self.IDLE_MIN)
                continue

            idle_time = self._compute_idle_time()
            logger.info(f"[vending] Waiting {idle_time:.0f}s for next customer")
            await asyncio.sleep(idle_time)

            # Customer presses a button
            button = self._pick_button()
            await self.publish(client, "hardware/buttons", ButtonPress(button=button))
            logger.info(f"[vending] Customer pressed button {button}")

            # Indecisive customer (20%): changes their mind
            if random.random() < 0.20:
                await asyncio.sleep(random.uniform(5.0, 15.0))
                other_buttons = [b for b in range(self.num_buttons) if b != button]
                if other_buttons:
                    button = random.choice(other_buttons)
                    await self.publish(
                        client, "hardware/buttons", ButtonPress(button=button)
                    )
                    logger.info(
                        f"[vending] Indecisive customer changed to button {button}"
                    )

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the button press, sensor monitoring, and dispense simulation."""
        logger.info("[vending] Starting vending machine simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._customer_loop(client))
            tg.create_task(self._publish_sensors(client))
            tg.create_task(self._command_loop(client))


if __name__ == "__main__":
    ESP32Simulator.entry_point(VendingMachineSimulator)
