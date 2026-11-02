# ice_sims/ice_maker.py
"""
Ice maker temperature monitoring simulator.

Models a simplified refrigeration cycle with 10 temperature sensors (including two hot gas valves).
The compressor cycles on/off and all temperatures respond with thermal lag.

Run: uv run python -m ice_sims.ice_maker [--broker HOST] [--port PORT] [--machine-id ID]
"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Literal

import aiomqtt
from loguru import logger

from ice_contracts.board.common import STANDARD_COMMANDS, SubsystemCommand
from ice_contracts.board.ice_maker_monitor import (
    CONTRACT_VERSION,
    ChannelDescriptor,
    ChannelReading,
    MonitorCapabilities,
)
from ice_sims.base import (
    CommandOutcome,
    ESP32Simulator,
    FaultDef,
    temperature_entity,
    uptime_entity,
)
from ice_common.build_info import BUILD_INFO
from ice_contracts.board.mqtt_messages import IceMakerEvent


# Sensor definitions: name, target when compressor on, target when off, rate constant, noise amplitude
SENSOR_DEFS = [
    {
        "name": "water_inlet",
        "target_on": 15.0,
        "target_off": 15.0,
        "rate": 0.01,
        "noise": 0.3,
    },
    {
        "name": "water_bath",
        "target_on": 1.5,
        "target_off": 8.0,
        "rate": 0.02,
        "noise": 0.1,
    },
    {
        "name": "compressor",
        "target_on": 70.0,
        "target_off": 30.0,
        "rate": 0.05,
        "noise": 0.5,
    },
    {
        "name": "exhaust_air",
        "target_on": 40.0,
        "target_off": 25.0,
        "rate": 0.03,
        "noise": 0.3,
    },
    {
        "name": "ambient_air",
        "target_on": 25.0,
        "target_off": 25.0,
        "rate": 0.005,
        "noise": 0.2,
    },
    {
        "name": "refrigerant_high",
        "target_on": 55.0,
        "target_off": 25.0,
        "rate": 0.06,
        "noise": 0.4,
    },
    {
        "name": "refrigerant_low",
        "target_on": -12.0,
        "target_off": 5.0,
        "rate": 0.04,
        "noise": 0.3,
    },
    {
        "name": "purge_water",
        "target_on": 3.0,
        "target_off": 7.0,
        "rate": 0.02,
        "noise": 0.2,
    },
    {
        "name": "hot_gas_valve_1",
        "target_on": 75.0,
        "target_off": 30.0,
        "rate": 0.04,
        "noise": 0.5,
    },
    {
        "name": "hot_gas_valve_2",
        "target_on": 75.0,
        "target_off": 30.0,
        "rate": 0.04,
        "noise": 0.5,
    },
]

TELEMETRY_CHANNELS = [
    ChannelDescriptor(
        channel_id="compressor_current",
        kind="current",
        unit="A",
        description="Compressor current draw",
        interval_seconds=5.0,
    ),
    ChannelDescriptor(
        channel_id="bin_level",
        kind="level",
        unit="%",
        description="Ice bin fill level",
        interval_seconds=5.0,
    ),
]

POWER_CYCLE_LOCKOUT_SECONDS = 300.0


class ThermalSensor:
    """Models a single temperature sensor with thermal lag toward a target."""

    def __init__(
        self, name: str, target_on: float, target_off: float, rate: float, noise: float
    ):
        self.name = name
        self.target_on = target_on
        self.target_off = target_off
        self.rate = rate
        self.noise = noise
        self._value = target_off  # start at off-state temperature

    @property
    def value(self) -> float:
        return self._value

    def update(self, compressor_on: bool, dt: float):
        """Move value toward the appropriate target with thermal lag and noise."""
        target = self.target_on if compressor_on else self.target_off
        # Exponential approach: value moves toward target at rate proportional to distance
        diff = target - self._value
        self._value += diff * self.rate * dt
        # Add random noise
        self._value += random.gauss(0, self.noise) * (dt**0.5)


@dataclass(frozen=True)
class SensorFault:
    """One ice-maker fault: pin `sensor` to `pin_value`, optionally reporting a halt."""

    fault: str
    category: Literal["short", "medium", "long"]
    probability: float
    message: str
    severity: str
    sensor: str
    pin_value: float
    halt_detail: str | None = None  # None: the machine never self-reports
    stops_compressor: bool = False


FAULT_TABLE: tuple[SensorFault, ...] = (
    SensorFault(
        "compressor_overtemp",
        "long",
        0.0005,
        "Compressor temp exceeded safe limit — service required",
        "critical",
        "refrigerant_high",
        95.0,
        "compressor_overtemp",
        stops_compressor=True,
    ),
    SensorFault(
        "low_refrigerant",
        "long",
        0.0003,
        "Low refrigerant detected — cooling ineffective",
        "critical",
        "refrigerant_low",
        10.0,
        "low_refrigerant",
    ),
    SensorFault(
        "water_inlet_blocked",
        "medium",
        0.0008,
        "Water inlet appears blocked — water bath not cooling",
        "warning",
        "water_bath",
        20.0,
        "water_inlet_blocked",
    ),
    # The machine has no fault detection for a stuck hot gas valve: the
    # valve temperature is pinned and nothing is published.
    *(
        SensorFault(
            f"defrost_stuck_{valve}",
            "short",
            0.0015,
            f"Hot gas valve {valve} stuck — harvest failing on evaporator {valve}",
            "warning",
            f"hot_gas_valve_{valve}",
            95.0,
        )
        for valve in (1, 2)
    ),
)


class IceMakerSimulator(ESP32Simulator):
    """Simulates ice maker temperature monitoring with compressor cycling."""

    PUBLISH_INTERVAL = 5.0  # seconds between sensor publishes
    COMPRESSOR_ON_TIME = 600.0  # 10 minutes
    COMPRESSOR_OFF_TIME = 300.0  # 5 minutes

    ICE_DROP_INTERVAL = 900.0  # simulate an ice drop every ~15 minutes
    TEMP_LOW = -20.0  # out-of-bounds threshold low (matches HealthMonitor temp_min)
    TEMP_HIGH = 80.0  # out-of-bounds threshold high (matches HealthMonitor temp_max)

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="ice_maker", **kwargs)
        self.sensors = [ThermalSensor(**s) for s in SENSOR_DEFS]
        self.compressor_on = False
        self._cycle_elapsed = 0.0
        self._ice_drop_elapsed = 0.0
        self._next_harvest_valve = 1
        self._pending_events: list[IceMakerEvent] = []
        self._publish_interval = float(self.PUBLISH_INTERVAL)
        self._last_power_cycle = -1e9
        self._bin_level = 20.0
        self._power_cycle_task: asyncio.Task | None = None
        self.register_command("power_cycle", self._handle_power_cycle)
        self.register_command("set_interval", self._handle_set_interval)

        for row in FAULT_TABLE:
            self.register_fault(self._fault_def(row))

    def sensor_by_name(self, name: str) -> ThermalSensor:
        """Return the ThermalSensor with the given name."""
        return next(s for s in self.sensors if s.name == name)

    def _fault_def(self, row: "SensorFault") -> FaultDef:
        """A `FaultDef` that pins `row.sensor` to `row.pin_value` while active.

        With a `halt_detail` the board reports it (`halt`, then `resume` with
        `<detail>_cleared`); without one the machine is unaware and publishes
        nothing. The "Fault activated/cleared" log lines are the base class's.
        """
        original = next(d for d in SENSOR_DEFS if d["name"] == row.sensor)

        async def activate(client: aiomqtt.Client) -> None:
            if row.stops_compressor:
                self.compressor_on = False
            sensor = self.sensor_by_name(row.sensor)
            sensor.target_on = row.pin_value
            sensor.target_off = row.pin_value
            if row.halt_detail is not None:
                await self.publish(
                    client,
                    "ice_maker/event",
                    IceMakerEvent(event="halt", detail=row.halt_detail),
                )

        async def recover(client: aiomqtt.Client) -> None:
            sensor = self.sensor_by_name(row.sensor)
            sensor.target_on = original["target_on"]
            sensor.target_off = original["target_off"]
            if row.halt_detail is not None:
                await self.publish(
                    client,
                    "ice_maker/event",
                    IceMakerEvent(event="resume", detail=f"{row.halt_detail}_cleared"),
                )

        return FaultDef(
            name=row.fault,
            category=row.category,
            probability=row.probability,
            on_activate=activate,
            on_recover=recover,
            message=row.message,
            severity=row.severity,
        )

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for ice maker sensors."""
        entities = []
        # 10 temperature sensors — one per thermal sensor
        for sensor in self.sensors:
            entities.append(
                temperature_entity(
                    sensor.name,
                    f"Ice Maker {sensor.name.replace('_', ' ').title()} Temperature",
                    object_id=f"{sensor.name}_temp",
                )
            )
        # Compressor binary sensor
        entities.append(
            {
                "component": "binary_sensor",
                "object_id": "compressor",
                "name": "Ice Maker Compressor",
                "state_topic_suffix": "ice_maker/event",
                "value_template": "{{ 'ON' if value_json.event == 'power_on' else 'OFF' }}",
                "device_class": "running",
                "payload_on": "ON",
                "payload_off": "OFF",
            }
        )
        entities.append(uptime_entity("ice_maker", "Ice Maker Uptime"))
        return entities

    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        active = self.active_fault_names

        # compressor_overtemp: safety cutout — machine fully halted
        if "compressor_overtemp" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # water_inlet_blocked: no water — machine halted
        if "water_inlet_blocked" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # Normal cycling. low_refrigerant and defrost_stuck_1/2 do NOT halt
        # anything — this machine has no fault detection and keeps running.
        self._cycle_elapsed += dt
        cycle_time = (
            self.COMPRESSOR_ON_TIME if self.compressor_on else self.COMPRESSOR_OFF_TIME
        )
        if self._cycle_elapsed >= cycle_time:
            self.compressor_on = not self.compressor_on
            self._cycle_elapsed = 0.0
            event_type = "power_on" if self.compressor_on else "power_off"
            self._pending_events.append(IceMakerEvent(event=event_type))
            logger.info(
                f"[ice_maker] Compressor {'ON' if self.compressor_on else 'OFF'}"
            )

        for sensor in self.sensors:
            sensor.update(self.compressor_on, dt)

        self._check_temp_bounds()

        # Harvest attempts alternate evaporators and never pause for stuck
        # valves — a stuck valve's turn simply fails.
        self._ice_drop_elapsed += dt
        if self._ice_drop_elapsed >= self.ICE_DROP_INTERVAL:
            self._ice_drop_elapsed = 0.0
            valve = self._next_harvest_valve
            self._next_harvest_valve = 2 if valve == 1 else 1
            if f"defrost_stuck_{valve}" in active:
                self._pending_events.append(
                    IceMakerEvent(
                        event="failed_cycle",
                        detail=f"hot_gas_valve_{valve}_stuck",
                    )
                )
                logger.warning(
                    f"[ice_maker] Harvest FAILED on evaporator {valve} (valve stuck)"
                )
            else:
                self._pending_events.append(
                    IceMakerEvent(event="ice_dropped", detail=f"evaporator_{valve}")
                )
                self._bin_level = min(100.0, self._bin_level + 2.0)
                logger.info(f"[ice_maker] Ice dropped from evaporator {valve}")

    def _check_temp_bounds(self) -> None:
        """Append out-of-bounds events for any sensors outside safe range."""
        for sensor in self.sensors:
            if sensor.value < self.TEMP_LOW or sensor.value > self.TEMP_HIGH:
                self._pending_events.append(
                    IceMakerEvent(
                        event="temp_out_of_bounds",
                        detail=f"{sensor.name}={sensor.value:.1f}C",
                    )
                )

    def build_capabilities(self) -> MonitorCapabilities:
        """Copilot review (PR 22, id=4128088689): this override (a
        MonitorCapabilities, not the base's SubsystemCapabilities) listed
        its commands by hand and omitted `ping`/`self_test` even though
        the base class registers both handlers for every subsystem,
        including this one. Prepend STANDARD_COMMANDS the same way
        ESP32Simulator.build_capabilities does, so this list can't drift
        from what is actually registered and answered on the wire.
        `force_report` used to be listed by hand too; it now arrives via
        STANDARD_COMMANDS instead of being duplicated.
        """
        temp_channels = [
            ChannelDescriptor(
                channel_id=s.name,
                kind="temperature",
                unit="C",
                description=f"{s.name.replace('_', ' ')} temperature",
                interval_seconds=self._publish_interval,
            )
            for s in self.sensors
        ]
        compressor_channel = ChannelDescriptor(
            # Named distinctly from the "compressor" temperature sensor
            # above -- channel ids must be unique per board (coordinator
            # decision, 2026-09-30) -- this is the binary run state, not a
            # reading off that sensor.
            channel_id="compressor_run",
            kind="binary",
            description="Compressor on/off state",
            # Not on a publish timer -- the power_on/power_off events ARE
            # the signal (spec §4.2) -- so this is a nominal cadence, not a
            # real polling interval; 5.0 matches TELEMETRY_CHANNELS.
            interval_seconds=5.0,
            direction="output",
            driven_by=None,
        )
        return MonitorCapabilities(
            contract_version=CONTRACT_VERSION,
            brand="ice-colder",
            model="simulator",
            firmware=BUILD_INFO.commit_short,
            hardware_id=self.fake_hardware_id(),
            ip=self.container_ip(),
            channels=temp_channels + TELEMETRY_CHANNELS + [compressor_channel],
            commands=[*STANDARD_COMMANDS, "power_cycle", "set_interval"],
        )

    def _compressor_current(self) -> float:
        base = 8.5 if self.compressor_on else 0.4
        return round(base + random.gauss(0, 0.15), 2)

    async def _publish_snapshot(self, client: aiomqtt.Client):
        """Publish one full round of sensor + telemetry readings."""
        for sensor in self.sensors:
            await self.publish_temperature(client, sensor.name, sensor.value, qos=0)
        await self.publish(
            client,
            "telemetry/ice_maker/compressor_current",
            ChannelReading(
                channel_id="compressor_current", value=self._compressor_current()
            ),
            qos=0,
        )
        await self.publish(
            client,
            "telemetry/ice_maker/bin_level",
            ChannelReading(channel_id="bin_level", value=round(self._bin_level, 1)),
            qos=0,
        )

    async def _handle_power_cycle(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        """dwell_seconds is already validated (5-300) by COMMAND_PARAM_VALIDATORS.

        Completion-table amendment (2026-09-29): a lockout rejection is
        immediate (nothing was started, so `phase` stays "completed", the
        default) — but an accepted power cycle now acks `phase="accepted"`:
        `dwell_seconds` can be up to 300 s, and a tech's maintenance lease
        must stay held for the whole dwell, not just until it starts.
        Completion is a SECOND, `phase="completed"` ack on this same
        `cmd/ice_maker/ack` topic and `request_id`
        (`publish_completion_ack`), published once `_finish_power_cycle`'s
        sleep elapses — alongside the existing `power_cycled`
        `ice_maker/event`, which stays as real telemetry for any other
        listener; the ack is what `ice_edge/hal/command_dispatcher.py`'s
        completion-wait actually keys on.
        """
        now = time.monotonic()
        if now - self._last_power_cycle < POWER_CYCLE_LOCKOUT_SECONDS:
            return CommandOutcome(status="rejected", detail="lockout")

        self._last_power_cycle = now
        dwell = cmd.params["dwell_seconds"]
        self.compressor_on = False
        self._cycle_elapsed = 0.0
        self._pending_events.append(
            IceMakerEvent(event="power_off", detail="commanded power_cycle")
        )
        self._power_cycle_task = self._spawn_background(
            self._finish_power_cycle(client, cmd, dwell)
        )
        return CommandOutcome(
            status="ok", detail=f"dwell {dwell:.0f}s", phase="accepted"
        )

    async def _handle_set_interval(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> None:
        """interval_seconds is already validated (1-3600) by COMMAND_PARAM_VALIDATORS."""
        self._publish_interval = float(cmd.params["interval_seconds"])
        await self.publish(
            client,
            "capabilities/ice_maker",
            self.build_capabilities(),
            retain=True,
        )
        return None

    async def _force_report_extra(self, client: aiomqtt.Client) -> None:
        """force_report republishes sensors + telemetry, on top of the heartbeat."""
        await self._publish_snapshot(client)

    async def _finish_power_cycle(
        self, client: aiomqtt.Client, cmd: SubsystemCommand, dwell: float
    ):
        await asyncio.sleep(dwell)
        self._pending_events.append(
            IceMakerEvent(event="power_cycled", detail="power restored")
        )
        logger.info("[ice_maker] Power cycle complete")
        await self.publish_completion_ack(client, cmd, {"dwell_seconds": dwell})

    async def run_simulation(self, client: aiomqtt.Client):
        """Publish startup event, then readings/events; handle contract commands.

        Capabilities are published by the base class on connect (after HA
        discovery); this only re-publishes them on set_interval.
        """
        logger.info("[ice_maker] Starting temperature monitoring simulation")
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="power_on", detail="simulator started"),
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.command_loop(client))
            tg.create_task(self._publish_loop(client))

    async def _publish_loop(self, client: aiomqtt.Client):
        while True:
            self.tick(self._publish_interval)
            await self._publish_snapshot(client)
            for event in self._pending_events:
                await self.publish(client, "ice_maker/event", event)
            self._pending_events.clear()
            await asyncio.sleep(self._publish_interval)


if __name__ == "__main__":
    ESP32Simulator.entry_point(IceMakerSimulator)
