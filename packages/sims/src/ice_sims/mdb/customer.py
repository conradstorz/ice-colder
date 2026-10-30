# ice_sims/mdb/customer.py
"""Simulated cash customer for the MDB board.

A real board knows nothing about products; the customer reads the selected
product's price from the edge's retained `status` and pays toward it with the
denominations the policy enables.
"""

import random
from typing import Protocol

from loguru import logger

OVERPAY_PROBABILITY = 0.05
LEVER_PROBABILITY = 0.03
MAX_INSERTIONS = 40


class CustomerPort(Protocol):
    @property
    def accepting(self) -> bool: ...

    @property
    def coin_enabled(self) -> list[int]: ...

    @property
    def bill_enabled(self) -> list[int]: ...

    @property
    def escrow_cents(self) -> int: ...

    def interacting(self) -> bool: ...

    def price_cents_for(self, product_name: str) -> int | None: ...

    async def next_status(self) -> dict: ...

    async def insert_coin(self, cents: int) -> bool: ...

    async def insert_bill(self, cents: int) -> bool: ...

    async def stack_bill(self) -> None: ...

    async def press_lever(self) -> None: ...

    async def sleep(self, seconds: float) -> None: ...


class MdbCustomer:
    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        think_range: tuple[float, float] = (2.0, 5.0),
        step_range: tuple[float, float] = (0.3, 1.0),
    ) -> None:
        self._rng = rng or random.Random()
        self._think = think_range
        self._step = step_range

    def plan(
        self,
        remaining_cents: int,
        coins: list[int],
        bills: list[int],
        *,
        use_bill: bool,
        overpay: bool,
    ) -> list[tuple[str, int]]:
        """Insertions that cover `remaining_cents` (plus a little, if overpaying)."""
        if remaining_cents <= 0:
            return []
        need = remaining_cents + (self._rng.choice([25, 50, 100]) if overpay else 0)
        if use_bill and bills:
            ordered = sorted(bills)
            fitting = [b for b in ordered if b >= need]
            if fitting:
                return [("bill", fitting[0])]
            steps: list[tuple[str, int]] = []
            while need > 0 and len(steps) < MAX_INSERTIONS:
                steps.append(("bill", ordered[-1]))
                need -= ordered[-1]
            return steps
        if not coins:
            return []
        ordered = sorted(coins)
        steps = []
        while need > 0 and len(steps) < MAX_INSERTIONS:
            fitting = [c for c in ordered if c <= need]
            coin = fitting[-1] if fitting else ordered[0]
            steps.append(("coin", coin))
            need -= coin
        return steps

    async def pay(self, port: CustomerPort, price_cents: int) -> None:
        if self._rng.random() < LEVER_PROBABILITY:
            await port.press_lever()
            return
        steps = self.plan(
            price_cents - port.escrow_cents,
            port.coin_enabled,
            port.bill_enabled,
            use_bill=self._rng.random() < 0.5,
            overpay=self._rng.random() < OVERPAY_PROBABILITY,
        )
        for kind, cents in steps:
            if not port.interacting() or not port.accepting:
                return
            if kind == "coin":
                took = await port.insert_coin(cents)
            else:
                took = await port.insert_bill(cents)
                if took:
                    await port.sleep(self._rng.uniform(*self._step))
                    await port.stack_bill()
            if not took:
                logger.info(f"[mdb] {kind} of {cents} cents refused; customer stops")
                return
            await port.sleep(self._rng.uniform(*self._step))

    async def run(self, port: CustomerPort) -> None:
        while True:
            status = await port.next_status()
            if status.get("state") != "interacting_with_user":
                continue
            if not port.accepting:
                logger.debug("[mdb] Interaction seen but board not accepting; waiting")
                continue
            selected = status.get("selected_product")
            if not selected:
                continue
            price = port.price_cents_for(selected)
            if price is None:
                price = 300
            logger.info(f"[mdb] Customer interaction: {selected} ({price} cents)")
            await port.sleep(self._rng.uniform(*self._think))
            await self.pay(port, price)
