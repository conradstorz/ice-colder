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
    ESP32Simulator.entry_point(VendingMachineSimulator)
