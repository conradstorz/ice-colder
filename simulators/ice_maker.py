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

from simulators.base import ESP32Simulator
from services.mqtt_messages import SensorReading


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

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="ice_maker", **kwargs)
        self.sensors = [ThermalSensor(**s) for s in SENSOR_DEFS]
        self.compressor_on = False
        self._cycle_elapsed = 0.0

    def tick(self, dt: float):
        """Advance the simulation by dt seconds."""
        self._cycle_elapsed += dt
        cycle_time = self.COMPRESSOR_ON_TIME if self.compressor_on else self.COMPRESSOR_OFF_TIME
        if self._cycle_elapsed >= cycle_time:
            self.compressor_on = not self.compressor_on
            self._cycle_elapsed = 0.0
            state = "ON" if self.compressor_on else "OFF"
            logger.info(f"[ice_maker] Compressor {state}")

        for sensor in self.sensors:
            sensor.update(self.compressor_on, dt)

    async def run_simulation(self, client: aiomqtt.Client):
        """Publish temperature readings every PUBLISH_INTERVAL seconds."""
        logger.info("[ice_maker] Starting temperature monitoring simulation")
        while True:
            self.tick(self.PUBLISH_INTERVAL)
            for sensor in self.sensors:
                reading = SensorReading(
                    location=sensor.name,
                    value=round(sensor.value, 2),
                )
                await self.publish(client, f"sensors/temp/{sensor.name}", reading)
            await asyncio.sleep(self.PUBLISH_INTERVAL)


if __name__ == "__main__":
    ESP32Simulator.entry_point(IceMakerSimulator)
