# tests/test_vmc_observers.py
"""Tests for Task 9 (observers and the test-sale seams on the VMC):

- `subscribe_state_change`/`subscribe_sale_settled` -- the two observer
  lists `_after_state_change` and the three settled-notification sites
  (`on_dispenser_event`'s success/failure branches, `on_dispense_failed`)
  broadcast to, synchronously, in subscription order, with a raising
  observer logged and never blocking the rest.
- `begin_test_sale`/`end_test_sale`/`find_product` -- the public seams
  `run_test_sale` (and, after Task 10, `TestSaleRunner`) now uses instead
  of the VMC-private waiter/path instance attributes it used to keep.

Drives real sales (through dispenser profiles and a `FakeDispatcher`,
mirroring `tests/test_vmc_dispense_profiles.py`'s own `_vmc_with_profiles`
helper) rather than a stub, so the settled notification is proven against
the real `on_dispenser_event`/`on_dispense_failed` call sites.
"""

import asyncio

import pytest

from config.config_model import ConfigModel, PhysicalDetails, Product
from contracts.vending_machine import FaultCode
from controller.machine import Machine
from tests.dispenser_fixtures import FakeDispatcher, profiles_for
from tests.fakes import FakeTaskRunner

ICE_1 = Product(sku="ICE-1", slot=0, kind="ice")
WATER_1 = Product(sku="WATER-1", slot=1, kind="water")


class FakeMQTT:
    """Minimal MQTT client for `Machine.set_mqtt_client` -- `register` is
    a no-op (the real client's subscription hook, called once per entry
    in `controller/mqtt_inbound.py`'s `SUBSCRIPTIONS` table; `tests.fakes.
    FakeMqtt` has no `register` at all, so it cannot stand in for a full
    `Machine` wiring, only for lower-level collaborator tests) and
    `publish` records every call, mirroring
    `tests/test_vmc_dispense_profiles.py`'s own local `FakeMQTT`."""

    def __init__(self):
        self.published: list[tuple[str, object]] = []

    def register(self, *a, **k):
        pass

    async def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload))


def _vmc_with_profiles(tmp_path, products=(ICE_1, WATER_1)):
    """A `Machine`/`VMC` wired for a real dispatch: loaded dispenser
    profiles, a `FakeDispatcher`, a `FakeMqtt`, and a `FakeTaskRunner` (so
    `dispense_timeout`/`process_payment` can be fired by label, exactly
    like the `vmc_fake_time`/`machine_fake_time` fixtures in
    `tests/conftest.py`) -- mirrors
    `tests/test_vmc_dispense_profiles.py`'s own `_vmc_with_profiles`,
    copied here per the task brief rather than inventing a new fixture,
    since that file's generic fixtures carry no products/profiles of
    their own."""
    cfg = ConfigModel(physical=PhysicalDetails(products=list(products)))
    runner = FakeTaskRunner()
    machine = Machine(config=cfg, tasks=runner)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    profiles = profiles_for(list(products), tmp_path)
    machine.set_dispenser_profiles(profiles)
    dispatcher = FakeDispatcher()
    machine.set_command_dispatcher(dispatcher)
    client = FakeMQTT()
    machine.set_mqtt_client(client)
    return machine, vmc, dispatcher, client, runner


def _start_sale(vmc, product) -> None:
    """Drive straight to 'dispensing' for a production sale, bypassing
    select_product's own FSM trigger (and so `_after_state_change`) --
    for tests that only care about the dispense-completion handlers, not
    the transition into 'dispensing' itself."""
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = product
    vmc.credit_escrow = product.price
    vmc.process_payment()
    assert vmc.state == "dispensing"


# --- subscribe_state_change ---


async def test_state_change_observer_sees_every_transition_in_order(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    seen: list[str] = []
    vmc.subscribe_state_change(seen.append)

    vmc.deposit_funds(product.price, payment_method="cash")
    vmc.select_product(0)  # idle -> interacting_with_user (real trigger)
    runner.fire("process_payment")  # interacting_with_user -> dispensing

    assert seen == ["interacting_with_user", "dispensing"]
    machine.cancel_pending_tasks()


async def test_state_change_observer_stops_after_unsubscribe(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    seen: list[str] = []
    unsubscribe = vmc.subscribe_state_change(seen.append)

    vmc.deposit_funds(product.price, payment_method="cash")
    vmc.select_product(0)
    assert seen == ["interacting_with_user"]

    unsubscribe()
    runner.fire("process_payment")  # interacting_with_user -> dispensing

    assert vmc.state == "dispensing"
    # The transition happened -- it is simply no longer observed.
    assert seen == ["interacting_with_user"]
    machine.cancel_pending_tasks()


async def test_state_change_observer_raising_does_not_block_the_next(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]

    def _raises(state: str) -> None:
        raise RuntimeError("boom")

    seen: list[str] = []
    vmc.subscribe_state_change(_raises)
    vmc.subscribe_state_change(seen.append)

    vmc.deposit_funds(product.price, payment_method="cash")
    vmc.select_product(0)

    assert seen == ["interacting_with_user"]
    machine.cancel_pending_tasks()


# --- subscribe_sale_settled ---


async def test_sale_settled_observer_fires_once_with_dispensed_on_complete(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    calls: list[tuple] = []
    vmc.subscribe_sale_settled(lambda *a: calls.append(a))
    _start_sale(vmc, product)

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )

    assert len(calls) == 1
    sale, outcome, fault_code = calls[0]
    assert outcome == "dispensed"
    assert fault_code is None
    assert sale.product is product
    machine.cancel_pending_tasks()


async def test_sale_settled_observer_fires_once_with_vend_failed_code_on_jam(
    tmp_path,
):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]  # ICE-1, bagged_ice -- jam maps to ICE-401
    calls: list[tuple] = []
    vmc.subscribe_sale_settled(lambda *a: calls.append(a))
    _start_sale(vmc, product)

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "jam"}
    )

    assert len(calls) == 1
    sale, outcome, fault_code = calls[0]
    assert outcome == "vend_failed"
    assert fault_code == "ICE-401"
    assert sale.product is product
    machine.cancel_pending_tasks()


async def test_sale_settled_observer_fires_once_with_timeout_on_dispense_timeout(
    tmp_path,
):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    calls: list[tuple] = []
    vmc.subscribe_sale_settled(lambda *a: calls.append(a))
    _start_sale(vmc, product)
    assert any(c.label == "dispense_timeout" for c in runner.scheduled)

    runner.fire("dispense_timeout")
    await asyncio.sleep(0)  # on_dispense_failed runs via fire_and_forget

    assert len(calls) == 1
    sale, outcome, fault_code = calls[0]
    assert outcome == "timeout"
    assert fault_code is None
    assert sale.product is product
    machine.cancel_pending_tasks()


async def test_sale_settled_observer_raising_does_not_block_the_next(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]

    def _raises(sale, outcome, fault_code) -> None:
        raise RuntimeError("boom")

    calls: list[tuple] = []
    vmc.subscribe_sale_settled(_raises)
    vmc.subscribe_sale_settled(lambda *a: calls.append(a))
    _start_sale(vmc, product)

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )

    assert len(calls) == 1
    machine.cancel_pending_tasks()


async def test_sale_settled_observer_never_fires_on_cancel_sale_or_reset(tmp_path):
    """Negative coverage (Task 9 review minor): `cancel_sale` (a catalog
    edit removing the selected product mid-session) and `reset_state`
    (recovery from `error`) both settle a sale by clearing it directly --
    neither is one of the three sites `_notify_settled` is called from
    (`on_dispenser_event`'s success/failure branches, `on_dispense_failed`)
    -- so the `subscribe_sale_settled` observer must never fire for
    either, unlike `subscribe_state_change`, which still sees every
    transition regardless."""
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    calls: list[tuple] = []
    vmc.subscribe_sale_settled(lambda *a: calls.append(a))

    vmc.deposit_funds(product.price, payment_method="cash")
    vmc.select_product(0)
    assert vmc.state == "interacting_with_user"

    vmc.cancel_sale()
    assert vmc.state == "idle"
    assert calls == []

    vmc.error_occurred()
    assert vmc.state == "error"
    vmc.reset_state()
    assert vmc.state == "idle"
    assert calls == []
    machine.cancel_pending_tasks()


# --- begin_test_sale / end_test_sale / find_product ---


async def test_find_product_returns_index_and_product_by_identity(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)

    index, product = vmc.find_product("WATER-1")
    assert index == 1
    assert product is vmc.products[1]

    assert vmc.find_product("NO-SUCH-SKU") == (None, None)
    machine.cancel_pending_tasks()


async def test_begin_test_sale_refuses_second_call_while_active(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]

    assert vmc.begin_test_sale(product) is True
    assert vmc.test_sale_in_progress is True

    with pytest.raises(RuntimeError, match="already in progress"):
        vmc.begin_test_sale(product)

    # The refused call must not have perturbed the first call's state.
    assert vmc.selected_product is product
    assert vmc.sale is not None and vmc.sale.is_test is True
    machine.cancel_pending_tasks()


async def test_begin_test_sale_returns_false_for_locked_sku(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    vmc.raise_fault(FaultCode.ICE_401, sku=product.sku)

    # select_product's lockout check returns before ever reaching
    # `self.selected_product = candidate` -- the FSM never leaves 'idle'.
    # The pre-seeded SaleContext (already carrying `product`, by identity)
    # is what makes `self.selected_product is product` still true here;
    # `begin_test_sale`'s own "and state == interacting_with_user" half of
    # the return expression is what correctly turns this into False.
    assert vmc.begin_test_sale(product) is False
    assert vmc.state == "idle"
    # begin_test_sale still set the guard -- end_test_sale (via
    # run_test_sale's own finally, in production) is what clears it.
    assert vmc.test_sale_in_progress is True

    vmc.end_test_sale()
    assert vmc.selected_product is None
    machine.cancel_pending_tasks()


async def test_end_test_sale_clears_context_when_not_dispensing(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    vmc.raise_fault(FaultCode.ICE_401, sku=product.sku)
    assert vmc.begin_test_sale(product) is False
    assert vmc.sale is not None  # seeded context, not yet cleaned up

    vmc.end_test_sale()

    assert vmc.sale is None
    assert vmc.test_sale_in_progress is False
    assert vmc.credit_escrow == 0.0
    machine.cancel_pending_tasks()


async def test_end_test_sale_leaves_context_while_still_dispensing(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]

    assert vmc.begin_test_sale(product) is True
    runner.fire("process_payment")
    assert vmc.state == "dispensing"

    vmc.end_test_sale()

    assert vmc.state == "dispensing"
    assert vmc.sale is not None
    assert vmc.sale.is_test is True
    assert vmc.test_sale_in_progress is False
    machine.cancel_pending_tasks()


# --- _fail_vend's unconditional test-credit clear (decision 3) ---


async def test_failed_test_vend_leaves_escrow_at_zero_with_no_refund_published(
    tmp_path,
):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]

    assert vmc.begin_test_sale(product) is True
    runner.fire("process_payment")
    assert vmc.state == "dispensing"

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "jam"}
    )

    assert vmc.credit_escrow == 0.0
    assert vmc.escrow_credits == []
    assert not any(topic == "cmd/payment/refund" for topic, *_ in client.published)

    vmc.end_test_sale()
    assert vmc.test_sale_in_progress is False
    machine.cancel_pending_tasks()
