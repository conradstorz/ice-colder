# simulators/ice_maker.py
"""
Ice maker temperature monitoring simulator.

Models a simplified refrigeration cycle with 9 temperature sensors.
The compressor cycles on/off and all temperatures respond with thermal lag.

Run: uv run python -m simulators.ice_maker [--broker HOST] [--port PORT] [--machine-id ID]
"""
import asyncio
import random

import aiomqtt
from loguru import logger

from simulators.base import ESP32Simulator, FaultDef
from services.mqtt_messages import SensorReading, IceMakerEvent


# Sensor definitions: name, target when compressor on, target when off, rate constant, noise amplitude
SENSOR_DEFS = [
    {"name": "water_inlet",      "target_on": 15.0,  "target_off": 15.0,  "rate": 0.01, "noise": 0.3},
    {"name": "water_bath",       "target_on": 1.5,   "target_off": 8.0,   "rate": 0.02, "noise": 0.1},
    {"name": "compressor",       "target_on": 70.0,  "target_off": 30.0,  "rate": 0.05, "noise": 0.5},
    {"name": "exhaust_air",      "target_on": 40.0,  "target_off": 25.0,  "rate": 0.03, "noise": 0.3},
    {"name": "ambient_air",      "target_on": 25.0,  "target_off": 25.0,  "rate": 0.005,"noise": 0.2},
    {"name": "refrigerant_high", "target_on": 55.0,  "target_off": 25.0,  "rate": 0.06, "noise": 0.4},
    {"name": "refrigerant_low",  "target_on": -12.0, "target_off": 5.0,   "rate": 0.04, "noise": 0.3},
    {"name": "purge_water",      "target_on": 3.0,   "target_off": 7.0,   "rate": 0.02, "noise": 0.2},
    {"name": "hot_gas_valve",    "target_on": 75.0,  "target_off": 30.0,  "rate": 0.04, "noise": 0.5},
]


class ThermalSensor:
    """Models a single temperature sensor with thermal lag toward a target."""

    def __init__(self, name: str, target_on: float, target_off: float, rate: float, noise: float):
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
        self._value += random.gauss(0, self.noise) * (dt ** 0.5)


class IceMakerSimulator(ESP32Simulator):
    """Simulates ice maker temperature monitoring with compressor cycling."""

    PUBLISH_INTERVAL = 5.0    # seconds between sensor publishes
    COMPRESSOR_ON_TIME = 600.0   # 10 minutes
    COMPRESSOR_OFF_TIME = 300.0  # 5 minutes

    ICE_DROP_INTERVAL = 900.0  # simulate an ice drop every ~15 minutes
    TEMP_LOW = -20.0   # out-of-bounds threshold low (matches HealthMonitor temp_min)
    TEMP_HIGH = 80.0   # out-of-bounds threshold high (matches HealthMonitor temp_max)

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="ice_maker", **kwargs)
        self.sensors = [ThermalSensor(**s) for s in SENSOR_DEFS]
        self.compressor_on = False
        self._cycle_elapsed = 0.0
        self._ice_drop_elapsed = 0.0
        self._pending_events: list[IceMakerEvent] = []

        # Register faults
        self.register_fault(FaultDef(
            name="compressor_overtemp",
            category="long",
            probability=0.0005,
            on_activate=self._on_compressor_overtemp_activate,
            on_recover=self._on_compressor_overtemp_recover,
            message="Compressor temp exceeded safe limit — service required",
            severity="critical",
        ))
        self.register_fault(FaultDef(
            name="low_refrigerant",
            category="long",
            probability=0.0003,
            on_activate=self._on_low_refrigerant_activate,
            on_recover=self._on_low_refrigerant_recover,
            message="Low refrigerant detected — cooling ineffective",
            severity="critical",
        ))
        self.register_fault(FaultDef(
            name="water_inlet_blocked",
            category="medium",
            probability=0.0008,
            on_activate=self._on_water_inlet_blocked_activate,
            on_recover=self._on_water_inlet_blocked_recover,
            message="Water inlet appears blocked — water bath not cooling",
            severity="warning",
        ))
        self.register_fault(FaultDef(
            name="defrost_stuck",
            category="short",
            probability=0.0015,
            on_activate=self._on_defrost_stuck_activate,
            on_recover=self._on_defrost_stuck_recover,
            message="Defrost cycle stuck — hot gas valve elevated",
            severity="warning",
        ))

    def _sensor_by_name(self, name: str) -> ThermalSensor:
        """Return the ThermalSensor with the given name."""
        return next(s for s in self.sensors if s.name == name)

    async def _on_compressor_overtemp_activate(self, client: aiomqtt.Client) -> None:
        self.compressor_on = False
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = 95.0
        sensor.target_off = 95.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="compressor_overtemp"))
        logger.warning("[ice_maker] FAULT: compressor overtemp — halted")

    async def _on_compressor_overtemp_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_high")
        sensor = self._sensor_by_name("refrigerant_high")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="compressor_overtemp_cleared"))
        logger.info("[ice_maker] Fault cleared: compressor_overtemp")

    async def _on_low_refrigerant_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = 10.0
        sensor.target_off = 10.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="low_refrigerant"))
        logger.warning("[ice_maker] FAULT: low refrigerant — cooling ineffective")

    async def _on_low_refrigerant_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_low")
        sensor = self._sensor_by_name("refrigerant_low")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="low_refrigerant_cleared"))
        logger.info("[ice_maker] Fault cleared: low_refrigerant")

    async def _on_water_inlet_blocked_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = 20.0
        sensor.target_off = 20.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="water_inlet_blocked"))
        logger.warning("[ice_maker] FAULT: water inlet blocked")

    async def _on_water_inlet_blocked_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "water_bath")
        sensor = self._sensor_by_name("water_bath")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="water_inlet_blocked_cleared"))
        logger.info("[ice_maker] Fault cleared: water_inlet_blocked")

    async def _on_defrost_stuck_activate(self, client: aiomqtt.Client) -> None:
        sensor = self._sensor_by_name("hot_gas_valve")
        sensor.target_on = 95.0
        sensor.target_off = 95.0
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="halt", detail="defrost_stuck"))
        logger.warning("[ice_maker] FAULT: defrost stuck — hot gas valve elevated")

    async def _on_defrost_stuck_recover(self, client: aiomqtt.Client) -> None:
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve")
        sensor = self._sensor_by_name("hot_gas_valve")
        sensor.target_on = original["target_on"]
        sensor.target_off = original["target_off"]
        await self.publish(client, "ice_maker/event",
                           IceMakerEvent(event="resume", detail="defrost_stuck_cleared"))
        logger.info("[ice_maker] Fault cleared: defrost_stuck")

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for ice maker sensors."""
        entities = []
        # 9 temperature sensors — one per thermal sensor
        for sensor in self.sensors:
            entities.append({
                "component": "sensor",
                "object_id": f"{sensor.name}_temp",
                "name": f"Ice Maker {sensor.name.replace('_', ' ').title()} Temperature",
                "state_topic_suffix": f"sensors/temp/{sensor.name}",
                "value_template": "{{ value_json.value }}",
                "device_class": "temperature",
                "unit_of_measurement": "\u00b0C",
                "state_class": "measurement",
                "expire_after": 30,
            })
        # Compressor binary sensor
        entities.append({
            "component": "binary_sensor",
            "object_id": "compressor",
            "name": "Ice Maker Compressor",
            "state_topic_suffix": "ice_maker/event",
            "value_template": "{{ 'ON' if value_json.event == 'power_on' else 'OFF' }}",
            "device_class": "running",
            "payload_on": "ON",
            "payload_off": "OFF",
        })
        # Uptime sensor from heartbeat
        entities.append({
            "component": "sensor",
            "object_id": "uptime",
            "name": "Ice Maker Uptime",
            "state_topic_suffix": "heartbeat/ice_maker",
            "value_template": "{{ value_json.uptime_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "total_increasing",
        })
        return entities

    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        active = self._active_fault_names

        # compressor_overtemp: halt compressor entirely
        if "compressor_overtemp" in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # low_refrigerant: compressor still cycles but cooling is ineffective
        # (refrigerant_low target already overridden in on_activate — just run normally)
        # water_inlet_blocked / defrost_stuck: halt compressor cycling
        if active and "low_refrigerant" not in active:
            for sensor in self.sensors:
                sensor.update(self.compressor_on, dt)
            self._check_temp_bounds()
            return

        # Normal operation (or low_refrigerant only — compressor cycles)
        self._cycle_elapsed += dt
        cycle_time = self.COMPRESSOR_ON_TIME if self.compressor_on else self.COMPRESSOR_OFF_TIME
        if self._cycle_elapsed >= cycle_time:
            self.compressor_on = not self.compressor_on
            self._cycle_elapsed = 0.0
            event_type = "power_on" if self.compressor_on else "power_off"
            self._pending_events.append(IceMakerEvent(event=event_type))
            logger.info(f"[ice_maker] Compressor {'ON' if self.compressor_on else 'OFF'}")

        for sensor in self.sensors:
            sensor.update(self.compressor_on, dt)

        self._check_temp_bounds()

        # Periodic ice drops only when no fault active
        if not active:
            self._ice_drop_elapsed += dt
            if self._ice_drop_elapsed >= self.ICE_DROP_INTERVAL:
                self._ice_drop_elapsed = 0.0
                self._pending_events.append(IceMakerEvent(event="ice_dropped"))
                logger.info("[ice_maker] Ice dropped")

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

    async def run_simulation(self, client: aiomqtt.Client):
        """Publish temperature readings and operational events."""
        logger.info("[ice_maker] Starting temperature monitoring simulation")
        await self.publish(client, "ice_maker/event", IceMakerEvent(event="power_on", detail="simulator started"))
        while True:
            self.tick(self.PUBLISH_INTERVAL)
            for sensor in self.sensors:
                reading = SensorReading(
                    location=sensor.name,
                    value=round(sensor.value, 2),
                )
                await self.publish(client, f"sensors/temp/{sensor.name}", reading)
            # Publish any pending operational events
            for event in self._pending_events:
                await self.publish(client, "ice_maker/event", event)
            self._pending_events.clear()
            await asyncio.sleep(self.PUBLISH_INTERVAL)


if __name__ == "__main__":
    ESP32Simulator.entry_point(IceMakerSimulator)
