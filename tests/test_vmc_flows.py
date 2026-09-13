"""Unit tests for money-critical VMC flows (no broker required).

These paths were previously only covered by tests/test_integration_e2e.py,
which skips without a live MQTT broker.
"""

import asyncio

from config.config_model import ConfigModel, Product
from controller.vmc import VMC


def make_vmc(price: float = 2.50) -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="ICE-1", name="Ice Bag", price=price)]
    return VMC(config=cfg)


class FakeSoldOutInventory:
    def is_available(self, sku):
        return False

    def is_tracked(self, sku):
        return True

    def decrement(self, sku):
        pass

    def get_count(self, sku):
        return 0


async def test_dispenser_jam_refunds_and_enters_error():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.selected_product = vmc.products[0]
    vmc.machine.set_state("dispensing")
    vmc.credit_escrow = 0.0  # price already deducted before dispensing

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "jammed"}
    )

    assert vmc.state == "error"
    # Jam refunds the price into escrow; on_error then refunds escrow to customer.
    assert vmc.credit_escrow == 0.0
    assert any("refunded" in m.lower() for m in messages)


async def test_session_timeout_refunds_and_returns_to_idle():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc._session_timeout_seconds = 0.05
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.credit_escrow = 3.00
    vmc.start_interaction()

    await asyncio.sleep(0.3)

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0
    assert any("Refund" in m for m in messages)


async def test_insufficient_funds_waits_without_charging():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 1.00

    vmc._process_payment()

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == 1.00
    assert any("Insufficient funds" in m for m in messages)
    vmc.cancel_pending_tasks()  # cancel the scheduled 5s retry


async def test_sold_out_rejects_selection():
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_inventory_manager(FakeSoldOutInventory())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)

    vmc.select_product(0)

    assert vmc.state == "idle"
    assert any("sold out" in m.lower() for m in messages)


async def test_sufficient_funds_charges_and_dispenses():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == 2.50

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    assert vmc.state == "interacting_with_user"  # credit remains
    vmc.cancel_pending_tasks()


async def test_dispense_timeout_fallback_completes_transaction():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    # Simulate the 60s hardware-silence fallback firing
    vmc._finish_dispensing()
    assert vmc.state == "idle"  # no credit left
    vmc.cancel_pending_tasks()


async def test_product_deleted_mid_session_refunds_and_errors():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()

    assert vmc.state == "error"
    assert vmc.credit_escrow == 0.0  # full escrow refunded by on_error
    assert any("refunded" in m.lower() for m in messages)
