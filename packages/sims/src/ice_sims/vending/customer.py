# ice_sims/vending/customer.py
"""Simulated customer arrivals for the vending board.

A real board knows its own buttons, not the edge's product catalog, so the
customer presses ids from `BUTTON_CHANNELS`; a button the catalog does not map
exercises the edge's unknown-button path.
"""

import asyncio
import random
from datetime import datetime

from loguru import logger

from ice_contracts.board.mqtt_messages import ButtonPress
from ice_sims.vending.board import BoardPort
from ice_sims.vending.channels import BUTTON_CHANNELS


class Customers:
    """Generates the button-press traffic a real customer would."""

    def __init__(self, *, idle_min: float, idle_max: float) -> None:
        self.idle_min = idle_min
        self.idle_max = idle_max

    def pick_button(self, exclude: str | None = None) -> str:
        return random.choice([b for b in BUTTON_CHANNELS if b != exclude])

    def arrival_factor(self, hour: int | None = None) -> float:
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

    def compute_idle_time(
        self, active_faults: set[str], hour: int | None = None
    ) -> float:
        """Return idle wait time in seconds, accounting for faults and time-of-day."""
        fault_active = "auger_jam" in active_faults or "ice_bin_empty" in active_faults
        if fault_active:
            return random.uniform(5.0, 15.0)
        factor = self.arrival_factor(hour=hour)
        return random.uniform(self.idle_min, self.idle_max) * factor

    async def run(self, board: BoardPort) -> None:
        """Simulate customers pressing buttons.

        Production dispensing now arrives entirely through the command
        channel (`cmd/vending`, `handle_command` -> `_handle_dispense` ->
        `execute_profile`), running concurrently via `command_loop` --
        this loop no longer waits for, or itself runs, a dispense. It only
        generates the button-press traffic a real customer would (plus the
        occasional change-of-mind second press); the old "wait on the
        legacy dispense topic, then vend, maybe vend again" tail -- the
        impatient-customer timeout and the repeat-customer purchase --
        depended entirely on that now-deleted topic/queue and is gone
        with it.
        """
        while True:
            idle_time = self.compute_idle_time(board.active_faults())
            logger.info(f"[vending] Waiting {idle_time:.0f}s for next customer")
            await asyncio.sleep(idle_time)

            # Customer presses a button
            button = self.pick_button()
            await board.publish("hardware/buttons", ButtonPress(button=button))
            logger.info(f"[vending] Customer pressed button {button}")

            # Indecisive customer (20%): changes their mind
            if random.random() < 0.20:
                await asyncio.sleep(random.uniform(5.0, 15.0))
                button = self.pick_button(exclude=button)
                await board.publish("hardware/buttons", ButtonPress(button=button))
                logger.info(f"[vending] Indecisive customer changed to button {button}")
