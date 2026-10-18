# ice_sims/vending/customer.py
"""Simulated customer arrivals for the vending board.

Moved verbatim from the simulator class (int buttons, `num_buttons`,
catalog reload included); a later task replaces the int-based button model.
"""

import asyncio
import random
from collections.abc import Callable
from datetime import datetime

from loguru import logger

from ice_contracts.board.mqtt_messages import ButtonPress
from ice_sims.vending.board import BoardPort


class Customers:
    """Generates the button-press traffic a real customer would."""

    def __init__(
        self,
        *,
        num_buttons: int,
        reload_products: Callable[[], int],
        idle_min: float,
        idle_max: float,
    ) -> None:
        self.num_buttons = num_buttons
        # Reloads the config and returns the (possibly new) product count.
        self._reload_products = reload_products
        self.idle_min = idle_min
        self.idle_max = idle_max

    def pick_button(self) -> int:
        return random.randint(0, self.num_buttons - 1)

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
        warned_no_products = False
        while True:
            if self.num_buttons == 0:
                if not warned_no_products:
                    logger.warning(
                        "[vending] No products configured — no buttons to press"
                    )
                    warned_no_products = True
                products = self._reload_products()
                if products:
                    self.num_buttons = products
                    logger.info(
                        f"[vending] Products loaded: {self.num_buttons} products"
                    )
                await asyncio.sleep(self.idle_min)
                continue

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
                other_buttons = [b for b in range(self.num_buttons) if b != button]
                if other_buttons:
                    button = random.choice(other_buttons)
                    await board.publish("hardware/buttons", ButtonPress(button=button))
                    logger.info(
                        f"[vending] Indecisive customer changed to button {button}"
                    )
