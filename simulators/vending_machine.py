# simulators/vending_machine.py
"""
Vending machine interface simulator.

Simulates the physical vending hardware: product buttons, ice dispense
(auger motor, agitator motor, fan, bag full sensor, bag drop solenoid),
water dispense (water valve solenoid, water flow sensor), ice bin half-full
detector, cabinet temperature sensor, and cabinet heater relay.

Subscribes to dispense commands from the RPi and runs the appropriate
dispense sequence with realistic hardware state transitions.

Run: uv run python -m simulators.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
"""

import asyncio
import random
from datetime import datetime

import aiomqtt
from loguru import logger

from contracts.vending_machine import DispenserOutcome
from simulators.base import ESP32Simulator, FaultDef
from services.mqtt_messages import (
    ButtonPress,
    DispenserStatus,
    HardwareIO,
    SensorReading,
)


# Keywords that identify a product as water (case-insensitive check on name/sku)
_WATER_KEYWORDS = {"water", "gallon"}


def _classify_product(name: str, sku: str) -> str:
    """Classify a product as 'ice' or 'water' based on its name/sku."""
    lower = f"{name} {sku}".lower()
    return "water" if any(kw in lower for kw in _WATER_KEYWORDS) else "ice"


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
}

SENSOR_PUBLISH_INTERVAL = 10.0  # seconds between periodic sensor publishes


class VendingMachineSimulator(ESP32Simulator):
    """Simulates the vending machine button panel and dispenser hardware."""

    IDLE_MIN = 30.0  # min seconds between customers
    IDLE_MAX = 90.0  # max seconds between customers
    DISPENSE_TIMEOUT = 60.0  # seconds to wait for dispense command
    SUPPORTED_COMMANDS = ["dispense", "payment/enable"]
    BRAND = "ice-colder"
    MODEL = "vending-sim"

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="vending", **kwargs)
        self._apply_products(self.config.products)
        logger.info(f"[vending] {self.num_buttons} products: {self._slot_types}")
        self._dispense_command: asyncio.Queue = asyncio.Queue()
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
                "unit_of_measurement": "\u00b0C",
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

    def _apply_products(self, products) -> None:
        """(Re)build num_buttons and the slot->type map from a product list."""
        self.num_buttons = len(products)
        # Keyed by each product's stable `slot`, not its list position — the
        # real ESP32's motor wiring is fixed per slot, and list order can
        # change independently (e.g. a product deleted from the catalog).
        self._slot_types = {p.slot: _classify_product(p.name, p.sku) for p in products}

    def slot_type(self, slot: int) -> str:
        return self._slot_types.get(slot, "ice")

    def _pick_button(self) -> int:
        return random.randint(0, self.num_buttons - 1)

    async def _set_hw(self, client: aiomqtt.Client, device: str, state: bool):
        """Update hardware state and publish to MQTT."""
        self._hw[device] = state
        await self.publish(
            client, f"hardware/io/{device}", HardwareIO(device=device, state=state)
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

    async def _run_ice_dispense(self, client: aiomqtt.Client, slot: int):
        """Run ice dispense sequence with realistic hardware transitions."""
        active = self._active_fault_names

        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(slot=slot, state="motor_active"),
        )

        # Ice bin empty: report immediately and abort
        if "ice_bin_empty" in active:
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(
                client,
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=DispenserOutcome.bin_empty.value),
            )
            logger.warning(f"[vending] Slot {slot}: ice bin empty")
            return

        # Start agitator and fan first
        await self._set_hw(client, "agitator_motor", True)
        await self._set_hw(client, "fan", True)
        await asyncio.sleep(1.0)

        # Start auger to fill bag
        await self._set_hw(client, "auger_motor", True)
        logger.info(f"[vending] Slot {slot}: auger running, filling bag")

        if "auger_jam" in active:
            # Auger runs but bag never fills — time out after 90 seconds
            await asyncio.sleep(90.0)
            await self._set_hw(client, "auger_motor", False)
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(
                client,
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=DispenserOutcome.timeout.value),
            )
            logger.warning(f"[vending] Slot {slot}: auger jam — dispense timed out")
            return

        # Wait for bag to fill (simulated)
        fill_time = random.uniform(5.0, 12.0)
        await asyncio.sleep(fill_time)

        # Bag full sensor triggers
        await self._set_hw(client, "bag_full_sensor", True)
        await self._set_hw(client, "auger_motor", False)
        logger.info(f"[vending] Slot {slot}: bag full")

        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(slot=slot, state="fill_complete"),
        )

        await asyncio.sleep(0.5)

        if "bag_drop_solenoid_stuck" in active:
            # Solenoid fires but bag doesn't drop — bag_full_sensor stays True
            await self._set_hw(client, "bag_drop_solenoid", True)
            await asyncio.sleep(0.5)
            # bag_full_sensor intentionally NOT cleared
            await self._set_hw(client, "agitator_motor", False)
            await self._set_hw(client, "fan", False)
            await self.publish(
                client,
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=DispenserOutcome.jam.value),
            )
            logger.warning(f"[vending] Slot {slot}: bag drop solenoid stuck")
            return

        # Drop the bag normally
        await self._set_hw(client, "bag_drop_solenoid", True)
        await asyncio.sleep(0.5)
        await self._set_hw(client, "bag_drop_solenoid", False)
        await self._set_hw(client, "bag_full_sensor", False)
        logger.info(f"[vending] Slot {slot}: bag dropped")

        # Stop agitator and fan
        await self._set_hw(client, "agitator_motor", False)
        await self._set_hw(client, "fan", False)

        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(slot=slot, state=DispenserOutcome.complete.value),
        )
        logger.info(f"[vending] Slot {slot}: ice dispense complete")

    async def _run_water_dispense(self, client: aiomqtt.Client, slot: int):
        """Run water dispense sequence with valve and flow sensor."""
        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(slot=slot, state="solenoid_open"),
        )

        # Open valve
        await self._set_hw(client, "water_valve_solenoid", True)
        await self._set_hw(client, "water_flow_sensor", True)
        logger.info(f"[vending] Slot {slot}: water valve open, dispensing")

        # Simulate flow pulses
        pulse_seconds = random.randint(5, 10)
        for i in range(pulse_seconds):
            await asyncio.sleep(1.0)
            gallons_per_pulse = 0.1
            self._water_flow_total += gallons_per_pulse
            logger.debug(
                f"[vending] Slot {slot}: flow total {self._water_flow_total:.1f} gal"
            )

        if "water_valve_stuck_open" in self._active_fault_names:
            # Valve does not close — hardware io already set to True in on_activate
            # flow incrementing continues in _publish_sensors
            logger.warning(
                f"[vending] Slot {slot}: water valve stuck open after dispense"
            )
            await self.publish(
                client,
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=DispenserOutcome.complete.value),
            )
            return

        # Close valve normally
        await self._set_hw(client, "water_valve_solenoid", False)
        await self._set_hw(client, "water_flow_sensor", False)

        await self.publish(
            client,
            "hardware/dispenser",
            DispenserStatus(slot=slot, state=DispenserOutcome.complete.value),
        )
        logger.info(f"[vending] Slot {slot}: water dispense complete")

    async def _listen_for_commands(self, client: aiomqtt.Client):
        """Read dispense commands from the subscription queue."""
        topic = f"{self.topic_prefix}/cmd/dispense"
        cmd_queue = await self.subscribe(client, topic)
        logger.info(f"[vending] Listening for dispense commands on {topic}")
        while True:
            _topic, data = await cmd_queue.get()
            slot = data.get("slot")
            if slot is not None:
                logger.info(f"[vending] Received dispense command for slot {slot}")
                await self._dispense_command.put(slot)

    async def _customer_loop(self, client: aiomqtt.Client):
        """Simulate customers pressing buttons and waiting for dispense."""
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
                    self._apply_products(self.config.products)
                    logger.info(
                        f"[vending] Products loaded: {self.num_buttons} products: "
                        f"{self._slot_types}"
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

            # Impatient customer (15%): shorter timeout
            timeout = (
                random.uniform(10.0, 20.0)
                if random.random() < 0.15
                else self.DISPENSE_TIMEOUT
            )

            try:
                slot = await asyncio.wait_for(
                    self._dispense_command.get(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.info("[vending] Customer walked away")
                continue

            # Run the appropriate dispense sequence
            if self.slot_type(slot) == "water":
                await self._run_water_dispense(client, slot)
            else:
                await self._run_ice_dispense(client, slot)

            # Repeat customer (10%): buys again immediately
            if random.random() < 0.10:
                logger.info("[vending] Repeat customer buying again")
                repeat_button = self._pick_button()
                await self.publish(
                    client, "hardware/buttons", ButtonPress(button=repeat_button)
                )
                logger.info(f"[vending] Repeat customer pressed button {repeat_button}")
                try:
                    slot = await asyncio.wait_for(
                        self._dispense_command.get(),
                        timeout=self.DISPENSE_TIMEOUT,
                    )
                    if self.slot_type(slot) == "water":
                        await self._run_water_dispense(client, slot)
                    else:
                        await self._run_ice_dispense(client, slot)
                except asyncio.TimeoutError:
                    logger.info("[vending] Repeat customer walked away")

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the button press, sensor monitoring, and dispense simulation."""
        logger.info("[vending] Starting vending machine simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._listen_for_commands(client))
            tg.create_task(self._customer_loop(client))
            tg.create_task(self._publish_sensors(client))


if __name__ == "__main__":
    ESP32Simulator.entry_point(VendingMachineSimulator)
