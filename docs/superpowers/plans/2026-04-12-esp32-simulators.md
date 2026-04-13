# ESP32 Simulators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build three independent mock ESP32 processes that produce realistic MQTT traffic (temperatures, button presses, dispense sequences, payments) so the RPi VMC can be observed end-to-end without physical hardware.

**Architecture:** Three separate Python scripts in a `simulators/` package, each connecting to the MQTT broker as its own client. A shared base class handles MQTT connection, heartbeat, CLI args, and reconnection. Each simulator subclass implements its specific behavior in a `run_simulation()` coroutine.

**Tech Stack:** `aiomqtt` (already a dependency), `argparse` for CLI, existing Pydantic message schemas from `services/mqtt_messages.py`.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `simulators/__init__.py` | Package marker (empty) |
| `simulators/base.py` | `ESP32Simulator` ABC — MQTT connect, heartbeat loop, CLI arg parsing, reconnect |
| `simulators/ice_maker.py` | Ice maker temperature simulation with compressor cycling |
| `simulators/vending_machine.py` | Button presses, dispense command listener, dispense sequences |
| `simulators/mdb_gateway.py` | Payment device status, reactive payment insertion |
| `tests/test_simulator_base.py` | Tests for base class |
| `tests/test_simulator_ice_maker.py` | Tests for temperature model |
| `tests/test_simulator_vending.py` | Tests for dispense sequences |
| `tests/test_simulator_mdb.py` | Tests for payment logic |

---

### Task 1: Base Class — ESP32Simulator

**Files:**
- Create: `simulators/__init__.py`
- Create: `simulators/base.py`
- Create: `tests/test_simulator_base.py`

- [ ] **Step 1: Write failing tests for base class**

Create `tests/test_simulator_base.py`:

```python
# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simulators.base import ESP32Simulator


class ConcreteSimulator(ESP32Simulator):
    """Minimal concrete subclass for testing the ABC."""
    def __init__(self, **kwargs):
        super().__init__(subsystem_name="test_subsystem", **kwargs)
        self.simulation_ran = False

    async def run_simulation(self, client):
        self.simulation_ran = True
        # Just run briefly then stop
        await asyncio.sleep(0.1)


class TestInit:
    def test_default_args(self):
        sim = ConcreteSimulator()
        assert sim.subsystem_name == "test_subsystem"
        assert sim.broker == "localhost"
        assert sim.port == 1883
        assert sim.machine_id == "vmc-0000"

    def test_custom_args(self):
        sim = ConcreteSimulator(broker="10.0.0.1", port=1884, machine_id="vmc-0042")
        assert sim.broker == "10.0.0.1"
        assert sim.port == 1884
        assert sim.machine_id == "vmc-0042"

    def test_topic_prefix(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        assert sim.topic_prefix == "vmc/vmc-0001"


class TestHeartbeat:
    def test_heartbeat_payload(self):
        sim = ConcreteSimulator()
        payload = sim._build_heartbeat()
        assert payload["subsystem"] == "test_subsystem"
        assert "uptime_seconds" in payload
        assert isinstance(payload["uptime_seconds"], int)


class TestCLIParsing:
    def test_parse_defaults(self):
        args = ESP32Simulator.parse_args([])
        assert args.broker == "localhost"
        assert args.port == 1883
        assert args.machine_id == "vmc-0000"

    def test_parse_custom(self):
        args = ESP32Simulator.parse_args(["--broker", "10.0.0.1", "--port", "1884", "--machine-id", "vmc-0042"])
        assert args.broker == "10.0.0.1"
        assert args.port == 1884
        assert args.machine_id == "vmc-0042"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simulators'`

- [ ] **Step 3: Create package and implement base class**

Create `simulators/__init__.py` (empty file).

Create `simulators/base.py`:

```python
# simulators/base.py
"""
Base class for ESP32 simulator processes.

Handles MQTT connection, heartbeat publishing, CLI argument parsing,
and automatic reconnection. Subclasses implement run_simulation().
"""
import argparse
import asyncio
import json
import time
from abc import ABC, abstractmethod

import aiomqtt
from loguru import logger
from pydantic import BaseModel


class ESP32Simulator(ABC):
    """
    Abstract base for all ESP32 simulators.

    Subclass and implement run_simulation(client) with the device-specific
    behavior. The base class handles MQTT connect, heartbeat, and reconnect.
    """

    HEARTBEAT_INTERVAL = 10.0  # seconds

    def __init__(
        self,
        subsystem_name: str,
        broker: str = "localhost",
        port: int = 1883,
        machine_id: str = "vmc-0000",
    ):
        self.subsystem_name = subsystem_name
        self.broker = broker
        self.port = port
        self.machine_id = machine_id
        self._start_time = time.monotonic()

    @property
    def topic_prefix(self) -> str:
        return f"vmc/{self.machine_id}"

    def _build_heartbeat(self) -> dict:
        uptime = int(time.monotonic() - self._start_time)
        return {
            "subsystem": self.subsystem_name,
            "uptime_seconds": uptime,
        }

    async def _heartbeat_loop(self, client: aiomqtt.Client):
        """Publish heartbeat every HEARTBEAT_INTERVAL seconds."""
        topic = f"{self.topic_prefix}/heartbeat/{self.subsystem_name}"
        while True:
            payload = self._build_heartbeat()
            await client.publish(topic, json.dumps(payload))
            logger.debug(f"[{self.subsystem_name}] heartbeat: uptime={payload['uptime_seconds']}s")
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def publish(self, client: aiomqtt.Client, topic_suffix: str, payload: BaseModel | dict):
        """Publish a message to vmc/{machine_id}/{topic_suffix}."""
        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)
        await client.publish(full_topic, data)
        logger.debug(f"[{self.subsystem_name}] published to {full_topic}")

    @abstractmethod
    async def run_simulation(self, client: aiomqtt.Client) -> None:
        """Subclass implements device-specific simulation here."""

    async def run(self) -> None:
        """Main entry: connect, run heartbeat + simulation, reconnect on failure."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.broker,
                    port=self.port,
                    identifier=f"sim-{self.subsystem_name}",
                ) as client:
                    logger.info(
                        f"[{self.subsystem_name}] Connected to {self.broker}:{self.port}"
                    )
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._heartbeat_loop(client))
                        tg.create_task(self.run_simulation(client))
            except aiomqtt.MqttError as e:
                logger.error(f"[{self.subsystem_name}] MQTT error: {e}")
            except Exception as e:
                logger.error(f"[{self.subsystem_name}] Unexpected error: {e}")

            logger.info(f"[{self.subsystem_name}] Reconnecting in 5s...")
            await asyncio.sleep(5)

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments common to all simulators."""
        parser = argparse.ArgumentParser(description="ESP32 Simulator")
        parser.add_argument("--broker", default="localhost", help="MQTT broker host")
        parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
        parser.add_argument("--machine-id", default="vmc-0000", help="Machine ID")
        return parser.parse_args(argv)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_simulator_base.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/__init__.py simulators/base.py tests/test_simulator_base.py
git commit -m "feat: add ESP32Simulator base class with heartbeat, CLI args, and reconnect"
```

---

### Task 2: Ice Maker Simulator — Temperature Model

**Files:**
- Create: `simulators/ice_maker.py`
- Create: `tests/test_simulator_ice_maker.py`

- [ ] **Step 1: Write failing tests for temperature model**

Create `tests/test_simulator_ice_maker.py`:

```python
# tests/test_simulator_ice_maker.py
"""Tests for simulators/ice_maker.py — ice maker temperature simulation."""
import pytest

from simulators.ice_maker import IceMakerSimulator, ThermalSensor, SENSOR_DEFS


class TestThermalSensor:
    def test_initial_value(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.1, noise=0.0)
        # Initial value is target_off (compressor starts off)
        assert sensor.value == 30.0

    def test_moves_toward_target_on(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.5, noise=0.0)
        initial = sensor.value
        sensor.update(compressor_on=True, dt=1.0)
        # Should move toward 50.0 from 30.0
        assert sensor.value > initial

    def test_moves_toward_target_off(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.5, noise=0.0)
        sensor._value = 50.0  # start at on-target
        sensor.update(compressor_on=False, dt=1.0)
        # Should move toward 30.0 from 50.0
        assert sensor.value < 50.0

    def test_noise_adds_variation(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.0, noise=1.0)
        values = set()
        for _ in range(20):
            sensor.update(compressor_on=False, dt=1.0)
            values.add(round(sensor.value, 2))
        # With noise=1.0 and rate=0.0, values should vary
        assert len(values) > 1

    def test_rate_zero_stays_put_without_noise(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.0, noise=0.0)
        sensor.update(compressor_on=True, dt=1.0)
        assert sensor.value == 30.0  # no movement


class TestSensorDefs:
    def test_all_nine_sensors_defined(self):
        assert len(SENSOR_DEFS) == 9

    def test_expected_sensor_names(self):
        names = {s["name"] for s in SENSOR_DEFS}
        expected = {
            "water_inlet", "water_bath", "compressor", "exhaust_air",
            "ambient_air", "refrigerant_high", "refrigerant_low",
            "purge_water", "hot_gas_valve",
        }
        assert names == expected


class TestIceMakerSimulator:
    def test_creates_with_defaults(self):
        sim = IceMakerSimulator()
        assert sim.subsystem_name == "ice_maker"
        assert len(sim.sensors) == 9

    def test_compressor_starts_off(self):
        sim = IceMakerSimulator()
        assert sim.compressor_on is False

    def test_tick_updates_all_sensors(self):
        sim = IceMakerSimulator()
        initial_values = {s.name: s.value for s in sim.sensors}
        sim.tick(dt=5.0)
        # At least some sensors should have changed (noise)
        changed = sum(
            1 for s in sim.sensors
            if round(s.value, 4) != round(initial_values[s.name], 4)
        )
        assert changed > 0

    def test_compressor_cycles(self):
        sim = IceMakerSimulator()
        assert sim.compressor_on is False
        # Advance past the off-cycle (300s default)
        for _ in range(61):
            sim.tick(dt=5.0)  # 305 seconds
        assert sim.compressor_on is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_ice_maker.py -v`
Expected: FAIL — `ImportError: cannot import name 'IceMakerSimulator'`

- [ ] **Step 3: Implement ice maker simulator**

Create `simulators/ice_maker.py`:

```python
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
    args = ESP32Simulator.parse_args()
    sim = IceMakerSimulator(broker=args.broker, port=args.port, machine_id=args.machine_id)
    asyncio.run(sim.run())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_simulator_ice_maker.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/ice_maker.py tests/test_simulator_ice_maker.py
git commit -m "feat: add ice maker simulator with thermal model and compressor cycling"
```

---

### Task 3: Vending Machine Simulator

**Files:**
- Create: `simulators/vending_machine.py`
- Create: `tests/test_simulator_vending.py`

- [ ] **Step 1: Write failing tests for vending machine**

Create `tests/test_simulator_vending.py`:

```python
# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from simulators.vending_machine import VendingMachineSimulator


class TestInit:
    def test_creates_with_defaults(self):
        sim = VendingMachineSimulator()
        assert sim.subsystem_name == "vending"
        assert sim.num_buttons == 3

    def test_slot_types(self):
        sim = VendingMachineSimulator()
        assert sim.slot_type(0) == "ice"
        assert sim.slot_type(1) == "ice"
        assert sim.slot_type(2) == "water"


class TestDispenseSequence:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_correct_states(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_ice_dispense(client, slot=0)

        assert published_states == ["motor_active", "fill_complete", "complete"]

    @pytest.mark.asyncio
    async def test_water_dispense_publishes_correct_states(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=2)

        assert published_states[0] == "solenoid_open"
        assert published_states[-1] == "complete"


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = VendingMachineSimulator()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset({0, 1, 2})
        assert len(buttons) > 1  # should hit at least 2 of 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_vending.py -v`
Expected: FAIL — `ImportError: cannot import name 'VendingMachineSimulator'`

- [ ] **Step 3: Implement vending machine simulator**

Create `simulators/vending_machine.py`:

```python
# simulators/vending_machine.py
"""
Vending machine interface simulator.

Simulates customers pressing buttons and the dispense hardware responding.
Subscribes to dispense commands from the RPi and runs the appropriate
dispense sequence (ice fill or water meter).

Run: uv run python -m simulators.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
"""
import asyncio
import json
import random

import aiomqtt
from loguru import logger

from simulators.base import ESP32Simulator
from services.mqtt_messages import ButtonPress, DispenserStatus


# Slot-to-product-type mapping
SLOT_TYPES = {0: "ice", 1: "ice", 2: "water"}


class VendingMachineSimulator(ESP32Simulator):
    """Simulates the vending machine button panel and dispenser hardware."""

    IDLE_MIN = 30.0   # min seconds between customers
    IDLE_MAX = 90.0   # max seconds between customers
    DISPENSE_TIMEOUT = 60.0  # seconds to wait for dispense command

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="vending", **kwargs)
        self.num_buttons = 3
        self._dispense_command: asyncio.Queue = asyncio.Queue()

    def slot_type(self, slot: int) -> str:
        return SLOT_TYPES.get(slot, "ice")

    def _pick_button(self) -> int:
        return random.randint(0, self.num_buttons - 1)

    async def _run_ice_dispense(self, client: aiomqtt.Client, slot: int):
        """Run ice dispense sequence: motor -> fill -> release."""
        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="motor_active"))
        logger.info(f"[vending] Slot {slot}: motor active, filling bag")

        fill_time = random.uniform(5.0, 15.0)
        await asyncio.sleep(fill_time)

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="fill_complete"))
        logger.info(f"[vending] Slot {slot}: bag full, releasing")

        await asyncio.sleep(1.0)

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="complete"))
        logger.info(f"[vending] Slot {slot}: dispense complete")

    async def _run_water_dispense(self, client: aiomqtt.Client, slot: int):
        """Run water dispense sequence: solenoid open -> pulse counting -> close."""
        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="solenoid_open"))
        logger.info(f"[vending] Slot {slot}: solenoid open, dispensing water")

        pulse_seconds = random.randint(5, 10)
        for i in range(pulse_seconds):
            await asyncio.sleep(1.0)
            logger.debug(f"[vending] Slot {slot}: pulse count {(i + 1) * 12}")

        await self.publish(client, "hardware/dispenser",
                           DispenserStatus(slot=slot, state="complete"))
        logger.info(f"[vending] Slot {slot}: water dispense complete")

    async def _listen_for_commands(self, client: aiomqtt.Client):
        """Subscribe to dispense commands and put them on the queue."""
        topic = f"{self.topic_prefix}/cmd/dispense"
        await client.subscribe(topic)
        logger.info(f"[vending] Subscribed to {topic}")
        async for message in client.messages:
            try:
                data = json.loads(message.payload)
                slot = data.get("slot")
                if slot is not None:
                    logger.info(f"[vending] Received dispense command for slot {slot}")
                    await self._dispense_command.put(slot)
            except (json.JSONDecodeError, TypeError):
                pass

    async def _customer_loop(self, client: aiomqtt.Client):
        """Simulate customers pressing buttons and waiting for dispense."""
        while True:
            # Wait for next customer
            idle_time = random.uniform(self.IDLE_MIN, self.IDLE_MAX)
            logger.info(f"[vending] Waiting {idle_time:.0f}s for next customer")
            await asyncio.sleep(idle_time)

            # Customer presses a button
            button = self._pick_button()
            await self.publish(client, "hardware/buttons",
                               ButtonPress(button=button))
            logger.info(f"[vending] Customer pressed button {button}")

            # Wait for dispense command from RPi
            try:
                slot = await asyncio.wait_for(
                    self._dispense_command.get(),
                    timeout=self.DISPENSE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.info("[vending] No dispense command received, customer walked away")
                continue

            # Run the appropriate dispense sequence
            if self.slot_type(slot) == "water":
                await self._run_water_dispense(client, slot)
            else:
                await self._run_ice_dispense(client, slot)

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the button press and dispense simulation."""
        logger.info("[vending] Starting vending machine simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._listen_for_commands(client))
            tg.create_task(self._customer_loop(client))


if __name__ == "__main__":
    args = ESP32Simulator.parse_args()
    sim = VendingMachineSimulator(broker=args.broker, port=args.port, machine_id=args.machine_id)
    asyncio.run(sim.run())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_simulator_vending.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/vending_machine.py tests/test_simulator_vending.py
git commit -m "feat: add vending machine simulator with button presses and dispense sequences"
```

---

### Task 4: MDB Gateway Simulator

**Files:**
- Create: `simulators/mdb_gateway.py`
- Create: `tests/test_simulator_mdb.py`

- [ ] **Step 1: Write failing tests for MDB gateway**

Create `tests/test_simulator_mdb.py`:

```python
# tests/test_simulator_mdb.py
"""Tests for simulators/mdb_gateway.py — MDB payment gateway simulation."""
import pytest

from simulators.mdb_gateway import MDBGatewaySimulator, PaymentStrategy


class TestInit:
    def test_creates_with_defaults(self):
        sim = MDBGatewaySimulator()
        assert sim.subsystem_name == "mdb"

    def test_devices_list(self):
        sim = MDBGatewaySimulator()
        assert len(sim.devices) == 3
        names = {d["name"] for d in sim.devices}
        assert names == {"coin_acceptor", "bill_validator", "card_reader"}


class TestPaymentStrategy:
    def test_pick_method_returns_valid(self):
        strategy = PaymentStrategy()
        methods = {strategy.pick_method() for _ in range(100)}
        assert methods.issubset({"cash_coin", "cash_bill", "card", "nfc"})

    def test_coin_denomination_valid(self):
        strategy = PaymentStrategy()
        coins = {strategy.pick_coin() for _ in range(100)}
        assert coins.issubset({0.25, 0.50, 1.00})

    def test_bill_denomination_valid(self):
        strategy = PaymentStrategy()
        bills = {strategy.pick_bill() for _ in range(100)}
        assert bills.issubset({1.00, 5.00, 10.00, 20.00})

    def test_card_amount_for_price(self):
        strategy = PaymentStrategy()
        amounts = [strategy.card_amount(3.00) for _ in range(100)]
        # All should be positive
        assert all(a > 0 for a in amounts)
        # At least some should differ from 3.00
        unique = set(round(a, 2) for a in amounts)
        assert len(unique) > 1

```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_mdb.py -v`
Expected: FAIL — `ImportError: cannot import name 'MDBGatewaySimulator'`

- [ ] **Step 3: Implement MDB gateway simulator**

Create `simulators/mdb_gateway.py`:

```python
# simulators/mdb_gateway.py
"""
MDB payment gateway simulator.

Simulates MDB bus devices (coin acceptor, bill validator, card reader).
Watches VMC status via MQTT and reactively inserts payments when a
customer interaction is detected.

Run: uv run python -m simulators.mdb_gateway [--broker HOST] [--port PORT] [--machine-id ID]
"""
import asyncio
import json
import random

import aiomqtt
from loguru import logger

from simulators.base import ESP32Simulator
from services.mqtt_messages import PaymentEvent, PaymentStatus


class PaymentStrategy:
    """Encapsulates the randomized payment logic."""

    COIN_DENOMS = [0.25, 0.50, 1.00]
    BILL_DENOMS = [1.00, 5.00, 10.00, 20.00]
    METHODS = ["cash_coin", "cash_bill", "card", "nfc"]

    def pick_method(self) -> str:
        return random.choice(self.METHODS)

    def pick_coin(self) -> float:
        return random.choice(self.COIN_DENOMS)

    def pick_bill(self) -> float:
        return random.choice(self.BILL_DENOMS)

    def card_amount(self, price: float) -> float:
        """Return a card payment amount — sometimes exact, sometimes not."""
        roll = random.random()
        if roll < 0.4:
            # Underpay (partial auth)
            return round(price * random.uniform(0.3, 0.9), 2)
        elif roll < 0.8:
            # Exact or slight overpay
            return round(price * random.uniform(1.0, 1.1), 2)
        else:
            # Significant overpay
            return round(price * random.uniform(1.5, 3.0), 2)

class MDBGatewaySimulator(ESP32Simulator):
    """Simulates MDB payment devices reacting to VMC state."""

    DEVICE_STATUS_INTERVAL = 30.0  # seconds between device status publishes
    MAX_CASH_ATTEMPTS = 3

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="mdb", **kwargs)
        self.strategy = PaymentStrategy()
        self.devices = [
            {"name": "coin_acceptor", "state": "ready"},
            {"name": "bill_validator", "state": "ready"},
            {"name": "card_reader", "state": "ready"},
        ]
        self._vmc_status: asyncio.Queue = asyncio.Queue()

    async def _publish_device_status(self, client: aiomqtt.Client):
        """Periodically publish device readiness status."""
        while True:
            for device in self.devices:
                await self.publish(
                    client, "payment/status",
                    PaymentStatus(device=device["name"], state=device["state"]),
                )
            logger.debug("[mdb] Published device status")
            await asyncio.sleep(self.DEVICE_STATUS_INTERVAL)

    async def _watch_vmc_status(self, client: aiomqtt.Client):
        """Subscribe to VMC status and forward state changes to the payment loop."""
        topic = f"{self.topic_prefix}/status"
        await client.subscribe(topic)
        logger.info(f"[mdb] Subscribed to {topic}")
        async for message in client.messages:
            try:
                data = json.loads(message.payload)
                await self._vmc_status.put(data)
            except (json.JSONDecodeError, TypeError):
                pass

    async def _payment_loop(self, client: aiomqtt.Client):
        """React to VMC state changes by inserting payments."""
        while True:
            # Wait for a status update
            status = await self._vmc_status.get()
            state = status.get("state", "")

            if state != "interacting_with_user":
                continue

            selected = status.get("selected_product")
            if not selected:
                continue

            logger.info(f"[mdb] Customer interaction detected, product: {selected}")

            # Simulate customer reaching for wallet
            await asyncio.sleep(random.uniform(2.0, 5.0))

            method = self.strategy.pick_method()
            logger.info(f"[mdb] Payment method: {method}")

            if method in ("card", "nfc"):
                await self._do_card_payment(client, method)
            else:
                await self._do_cash_payment(client, method)

    async def _do_cash_payment(self, client: aiomqtt.Client, method: str):
        """Insert cash denominations, possibly requiring multiple attempts."""
        for attempt in range(self.MAX_CASH_ATTEMPTS):
            if method == "cash_coin":
                amount = self.strategy.pick_coin()
            else:
                amount = self.strategy.pick_bill()

            await self.publish(
                client, "payment/credit",
                PaymentEvent(amount=amount, method=method),
            )
            logger.info(f"[mdb] Inserted ${amount:.2f} via {method} (attempt {attempt + 1})")

            # Wait and check if VMC moved past interacting state
            await asyncio.sleep(random.uniform(3.0, 8.0))

            # Drain the queue to get latest status
            latest = None
            while not self._vmc_status.empty():
                try:
                    latest = self._vmc_status.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if latest and latest.get("state") != "interacting_with_user":
                logger.info("[mdb] VMC moved on, payment sufficient")
                return

        logger.info("[mdb] Max cash attempts reached")

    async def _do_card_payment(self, client: aiomqtt.Client, method: str):
        """Insert a card/NFC payment — single transaction."""
        # Use a reasonable default price estimate since we can't see the exact price
        # The VMC will handle insufficient funds
        amount = self.strategy.card_amount(3.00)
        await self.publish(
            client, "payment/credit",
            PaymentEvent(amount=amount, method=method),
        )
        logger.info(f"[mdb] Card/NFC payment: ${amount:.2f} via {method}")

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the MDB gateway simulation."""
        logger.info("[mdb] Starting MDB gateway simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._publish_device_status(client))
            tg.create_task(self._watch_vmc_status(client))
            tg.create_task(self._payment_loop(client))


if __name__ == "__main__":
    args = ESP32Simulator.parse_args()
    sim = MDBGatewaySimulator(broker=args.broker, port=args.port, machine_id=args.machine_id)
    asyncio.run(sim.run())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_simulator_mdb.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add simulators/mdb_gateway.py tests/test_simulator_mdb.py
git commit -m "feat: add MDB gateway simulator with reactive payment insertion"
```

---

### Task 5: Add `__main__.py` Entry Points

**Files:**
- Create: `simulators/__main__.py` (optional convenience runner)

- [ ] **Step 1: Create `__main__.py` files for each simulator**

These allow `uv run python -m simulators.ice_maker` etc. The `if __name__ == "__main__"` blocks in each simulator file already handle this, but let's verify they work.

Create `simulators/__main__.py` as a convenience that prints usage:

```python
# simulators/__main__.py
"""
ESP32 Simulator package.

Run individual simulators:
    uv run python -m simulators.ice_maker [--broker HOST] [--port PORT] [--machine-id ID]
    uv run python -m simulators.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
    uv run python -m simulators.mdb_gateway [--broker HOST] [--port PORT] [--machine-id ID]
"""
print(__doc__)
```

- [ ] **Step 2: Verify each simulator's `--help` works**

Run these three commands (each should print argparse help and exit):

```
uv run python -m simulators.ice_maker --help
uv run python -m simulators.vending_machine --help
uv run python -m simulators.mdb_gateway --help
```

Expected: Each prints `usage: __main__.py [-h] [--broker BROKER] [--port PORT] [--machine-id MACHINE_ID]` and exits cleanly.

- [ ] **Step 3: Commit**

```bash
git add simulators/__main__.py
git commit -m "feat: add simulator package entry point with usage instructions"
```

---

### Task 6: Full Test Suite Verification and PLAN.md Update

**Files:**
- Modify: `PLAN.md`

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (108 existing + ~27 new simulator tests = ~135 total)

- [ ] **Step 2: Update PLAN.md**

Add a new Phase 6 section (or append to existing plan) documenting the simulators:

```markdown
### Phase 6: ESP32 Simulators

Mock ESP32 processes for end-to-end testing without hardware.

- [x] Create ESP32Simulator base class (MQTT connect, heartbeat, CLI args, reconnect)
- [x] Create ice maker simulator (9 thermal sensors, compressor cycling)
- [x] Create vending machine simulator (button presses, ice/water dispense sequences)
- [x] Create MDB gateway simulator (reactive payment insertion, device status)
- [x] Add __main__.py entry points for each simulator
- [x] Write tests for all simulators (N tests, M total passing)
```

- [ ] **Step 3: Commit**

```bash
git add PLAN.md
git commit -m "docs: update PLAN.md with Phase 6 ESP32 simulators"
```
