# simulators/ice_maker.py
"""
Ice maker temperature monitoring simulator.

Models a simplified refrigeration cycle with 10 temperature sensors (including two hot gas valves).
The compressor cycles on/off and all temperatures respond with thermal lag.

Run: uv run python -m simulators.ice_maker [--broker HOST] [--port PORT] [--machine-id ID]
"""

import asyncio
import random
import time

import aiomqtt
from loguru import logger
from pydantic import ValidationError

from contracts.ice_maker_monitor import (
    CONTRACT_VERSION,
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)
from simulators.base import ESP32Simulator, FaultDef
from services.mqtt_messages import SensorReading, IceMakerEvent


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
        self._acked: dict[str, CommandAck] = {}
        self._bin_level = 20.0
        self._power_cycle_task: asyncio.Task | None = None

        # Register faults
        self.register_fault(
            FaultDef(
                name="compressor_overtemp",
                category="long",
                probability=0.0005,
                on_activate=self._on_compressor_overtemp_activate,
                on_recover=self._on_compressor_overtemp_recover,
                message="Compressor temp exceeded safe limit — service required",
                severity="critical",
            )
        )
        self.register_fault(
            FaultDef(
                name="low_refrigerant",
                category="long",
                probability=0.0003,
                on_activate=self._on_low_refrigerant_activate,
                on_recover=self._on_low_refrigerant_recover,
                message="Low refrigerant detected — cooling ineffective",
                severity="critical",
            )
        )
        self.register_fault(
            FaultDef(
                name="water_inlet_blocked",
                category="medium",
                probability=0.0008,
                on_activate=self._on_water_inlet_blocked_activate,
                on_recover=self._on_water_inlet_blocked_recover,
                message="Water inlet appears blocked — water bath not cooling",
                severity="warning",
            )
        )
        for valve in (1, 2):
            self.register_fault(
                FaultDef(
                    name=f"defrost_stuck_{valve}",
                    category="short",
                    probability=0.0015,
                    on_activate=self._make_valve_stuck_activate(valve),
                    on_recover=self._make_valve_stuck_recover(valve),
                    message=(
                        f"Hot gas valve {valve} stuck — "
                        f"harvest failing on evaporator {valve}"
                    ),
                    severity="warning",
                )
            )

    def _sensor_by_name(self, name: str) -> ThermalSensor:
        """Return the ThermalSensor with the given name."""
        return next(s for s in self.sensors if s.name == name)

    async def _on_compressor_overtemp_activate(self, client: aiomqtt.Client) -> None:
        self.compressor_on = False
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = 95.0
        sensor.target_off = 95.0
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="halt", detail="compressor_overtemp"),
        )
        logger.warning("[ice_maker] FAULT: compressor overtemp — halted")

    async def _on_compressor_overtemp_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_high")
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="resume", detail="compressor_overtemp_cleared"),
        )
        logger.info("[ice_maker] Fault cleared: compressor_overtemp")

    async def _on_low_refrigerant_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = 10.0
        sensor.target_off = 10.0
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="halt", detail="low_refrigerant"),
        )
        logger.warning("[ice_maker] FAULT: low refrigerant — cooling ineffective")

    async def _on_low_refrigerant_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_low")
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="resume", detail="low_refrigerant_cleared"),
        )
        logger.info("[ice_maker] Fault cleared: low_refrigerant")

    async def _on_water_inlet_blocked_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = 20.0
        sensor.target_off = 20.0
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="halt", detail="water_inlet_blocked"),
        )
        logger.warning("[ice_maker] FAULT: water inlet blocked")

    async def _on_water_inlet_blocked_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "water_bath")
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="resume", detail="water_inlet_blocked_cleared"),
        )
        logger.info("[ice_maker] Fault cleared: water_inlet_blocked")

    def _make_valve_stuck_activate(self, valve: int):
        """The machine has no fault detection: pin the valve temp, publish nothing."""

        async def _activate(client: aiomqtt.Client) -> None:
            sensor = self._sensor_by_name(f"hot_gas_valve_{valve}")
            sensor.target_on = 95.0
            sensor.target_off = 95.0
            logger.warning(
                f"[ice_maker] FAULT: hot gas valve {valve} stuck — "
                "machine unaware, still cycling"
            )

        return _activate

    def _make_valve_stuck_recover(self, valve: int):
        async def _recover(client: aiomqtt.Client) -> None:
            original = next(
                d for d in SENSOR_DEFS if d["name"] == f"hot_gas_valve_{valve}"
            )
            sensor = self._sensor_by_name(f"hot_gas_valve_{valve}")
            sensor.target_on = original["target_on"]
            sensor.target_off = original["target_off"]
            logger.info(f"[ice_maker] Fault cleared: defrost_stuck_{valve}")

        return _recover

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for ice maker sensors."""
        entities = []
        # 10 temperature sensors — one per thermal sensor
        for sensor in self.sensors:
            entities.append(
                {
                    "component": "sensor",
                    "object_id": f"{sensor.name}_temp",
                    "name": f"Ice Maker {sensor.name.replace('_', ' ').title()} Temperature",
                    "state_topic_suffix": f"sensors/temp/{sensor.name}",
                    "value_template": "{{ value_json.value }}",
                    "device_class": "temperature",
                    "unit_of_measurement": "\u00b0C",
                    "state_class": "measurement",
                    "expire_after": 30,
                }
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
        # Uptime sensor from heartbeat
        entities.append(
            {
                "component": "sensor",
                "object_id": "uptime",
                "name": "Ice Maker Uptime",
                "state_topic_suffix": "heartbeat/ice_maker",
                "value_template": "{{ value_json.uptime_seconds }}",
                "device_class": "duration",
                "unit_of_measurement": "s",
                "state_class": "total_increasing",
            }
        )
        return entities

    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        active = self._active_fault_names

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
        return MonitorCapabilities(
            contract_version=CONTRACT_VERSION,
            brand="ice-colder",
            model="simulator",
            firmware="sim",
            channels=temp_channels + TELEMETRY_CHANNELS,
            commands=["power_cycle", "force_report", "set_interval"],
        )

    def _compressor_current(self) -> float:
        base = 8.5 if self.compressor_on else 0.4
        return round(base + random.gauss(0, 0.15), 2)

    async def _publish_snapshot(self, client: aiomqtt.Client):
        """Publish one full round of sensor + telemetry readings."""
        for sensor in self.sensors:
            reading = SensorReading(location=sensor.name, value=round(sensor.value, 2))
            await self.publish(client, f"sensors/temp/{sensor.name}", reading, qos=0)
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

    async def _handle_command(self, client: aiomqtt.Client, cmd: MonitorCommand):
        if cmd.request_id in self._acked:
            await self.publish(client, "cmd/ice_maker/ack", self._acked[cmd.request_id])
            return

        if cmd.command == "power_cycle":
            now = time.monotonic()
            if now - self._last_power_cycle < POWER_CYCLE_LOCKOUT_SECONDS:
                ack = CommandAck(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    status="rejected",
                    detail="lockout",
                )
            else:
                self._last_power_cycle = now
                dwell = cmd.params["dwell_seconds"]
                self.compressor_on = False
                self._cycle_elapsed = 0.0
                self._pending_events.append(
                    IceMakerEvent(event="power_off", detail="commanded power_cycle")
                )
                self._power_cycle_task = asyncio.get_running_loop().create_task(
                    self._finish_power_cycle(dwell)
                )
                ack = CommandAck(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    status="ok",
                    detail=f"dwell {dwell:.0f}s",
                )
        elif cmd.command == "set_interval":
            self._publish_interval = float(cmd.params["interval_seconds"])
            await self.publish(
                client,
                "capabilities/ice_maker",
                self.build_capabilities(),
                retain=True,
            )
            ack = CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="ok"
            )
        else:  # force_report — validated Literal, only three commands exist
            await self._publish_snapshot(client)
            ack = CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="ok"
            )

        self._acked[cmd.request_id] = ack
        await self.publish(client, "cmd/ice_maker/ack", ack)
        logger.info(
            f"[ice_maker] Command {cmd.command} ({cmd.request_id}): {ack.status}"
        )

    async def _finish_power_cycle(self, dwell: float):
        await asyncio.sleep(dwell)
        self._pending_events.append(
            IceMakerEvent(event="power_cycled", detail="power restored")
        )
        logger.info("[ice_maker] Power cycle complete")

    async def _command_loop(self, client: aiomqtt.Client):
        queue = await self.subscribe(client, f"{self.topic_prefix}/cmd/ice_maker")
        while True:
            _, data = await queue.get()
            try:
                cmd = MonitorCommand.model_validate(data)
            except ValidationError as e:
                logger.warning(f"[ice_maker] Invalid command dropped: {e}")
                continue
            await self._handle_command(client, cmd)

    async def run_simulation(self, client: aiomqtt.Client):
        """Publish capabilities, then readings/events; handle contract commands."""
        logger.info("[ice_maker] Starting temperature monitoring simulation")
        await self.publish(
            client, "capabilities/ice_maker", self.build_capabilities(), retain=True
        )
        await self.publish(
            client,
            "ice_maker/event",
            IceMakerEvent(event="power_on", detail="simulator started"),
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._command_loop(client))
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
