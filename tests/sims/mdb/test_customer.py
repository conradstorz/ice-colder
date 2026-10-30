"""MdbCustomer: planning is pure; `run` is driven through a scripted port."""

import random

import pytest

from ice_sims.mdb.customer import MdbCustomer


def customer(seed=1):
    return MdbCustomer(rng=random.Random(seed))


def total(steps):
    return sum(c for _k, c in steps)


def test_coin_plan_covers_the_price_exactly_when_it_can():
    steps = customer().plan(
        300, [5, 10, 25, 100], [100, 500], use_bill=False, overpay=False
    )
    assert total(steps) == 300 and {k for k, _ in steps} == {"coin"}


def test_coin_plan_overshoots_by_less_than_the_smallest_coin():
    steps = customer().plan(302, [5, 25], [], use_bill=False, overpay=False)
    assert 302 <= total(steps) < 302 + 5


def test_bill_plan_uses_the_smallest_bill_that_covers():
    assert customer().plan(
        300, [25], [100, 500, 1000], use_bill=True, overpay=False
    ) == [("bill", 500)]


def test_bill_plan_stacks_several_when_none_covers():
    steps = customer().plan(1500, [25], [500, 1000], use_bill=True, overpay=False)
    assert total(steps) >= 1500 and {k for k, _ in steps} == {"bill"}


def test_overpay_adds_at_least_a_quarter():
    steps = customer().plan(300, [25, 100], [], use_bill=False, overpay=True)
    assert total(steps) >= 325


def test_nothing_to_plan_when_paid_or_nothing_enabled():
    assert customer().plan(0, [25], [100], use_bill=False, overpay=False) == []
    assert customer().plan(100, [], [], use_bill=False, overpay=False) == []
    assert customer().plan(100, [], [], use_bill=True, overpay=False) == []


class ScriptedPort:
    def __init__(self, statuses, *, accepting=True, coins=(25, 100), bills=(500,)):
        self._statuses = list(statuses)
        self.accepting = accepting
        self.coin_enabled = list(coins)
        self.bill_enabled = list(bills)
        self.escrow_cents = 0
        self.inserted: list[tuple[str, int]] = []
        self.levers = 0
        self.stacked = 0
        self.sleeps: list[float] = []

    def interacting(self):
        return True

    def price_cents_for(self, name):
        return {"Ice": 300}.get(name)

    async def next_status(self):
        if not self._statuses:
            raise StopAsyncIteration
        return self._statuses.pop(0)

    async def insert_coin(self, cents):
        self.inserted.append(("coin", cents))
        self.escrow_cents += cents
        return True

    async def insert_bill(self, cents):
        self.inserted.append(("bill", cents))
        return True

    async def stack_bill(self):
        self.stacked += 1
        self.escrow_cents += self.inserted[-1][1]

    async def press_lever(self):
        self.levers += 1

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


INTERACTING = {"state": "interacting_with_user", "selected_product": "Ice"}


async def test_run_pays_toward_the_selected_products_price():
    port = ScriptedPort([{"state": "idle"}, INTERACTING])
    with pytest.raises(StopAsyncIteration):
        await MdbCustomer(rng=_NoLever(2)).run(port)
    assert port.levers == 0
    assert port.escrow_cents >= 300


class _NoLever(random.Random):
    """Never rolls the lever or an overpay: random() is always high."""

    def random(self):
        return 0.99


async def test_run_ignores_a_board_that_is_not_accepting():
    port = ScriptedPort([INTERACTING], accepting=False)
    with pytest.raises(StopAsyncIteration):
        await MdbCustomer(rng=_NoLever(1)).run(port)
    assert port.inserted == []


class _AlwaysLever(random.Random):
    def random(self):
        return 0.0


async def test_the_customer_sometimes_presses_the_lever():
    port = ScriptedPort([INTERACTING])
    with pytest.raises(StopAsyncIteration):
        await MdbCustomer(rng=_AlwaysLever(1)).run(port)
    assert port.levers == 1 and port.inserted == []
