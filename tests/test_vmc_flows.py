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


async def test_late_dispenser_fault_after_completed_sale_is_ignored():
    """A duplicate/late 'jammed' MQTT message (QoS 0, no dedup) arriving after a
    sale has already completed must not credit a bogus refund or take the VMC
    offline — only a fault reported *during* dispensing is real."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    assert vmc.state == "idle"  # no credit left
    assert vmc.selected_product is None
    assert vmc.credit_escrow == 0.0

    # Late/duplicate jam for the same slot arrives after completion.
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "jammed"}
    )

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0  # no bogus refund credited
    vmc.cancel_pending_tasks()


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


async def test_product_deleted_mid_session_cancels_sale_without_error():
    """Deleting the selected product mid-session should cancel the sale and return
    the VMC to idle — not park it in error, which would take the machine offline
    for every subsequent customer over a benign catalog edit."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0  # refunded, same as on_reset/_expire_session
    assert vmc.selected_product is None
    assert any("refund" in m.lower() for m in messages)
    vmc.cancel_pending_tasks()


async def test_sale_cancelled_then_new_sale_succeeds():
    """After a cancelled sale, the VMC should be immediately usable again — no
    admin reset required, unlike a hardware fault that goes through error_occurred."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()
    assert vmc.state == "idle"

    # A new product is configured; a normal sale should work with no admin reset.
    new_product = Product(sku="ICE-2", name="Ice Bag 2", price=2.50)
    vmc.products.append(new_product)
    vmc.select_product(0)
    assert vmc.state == "interacting_with_user"
    assert vmc.selected_product is new_product

    vmc.credit_escrow = 2.50
    vmc._process_payment()
    assert vmc.state == "dispensing"
    vmc.cancel_pending_tasks()


async def test_dispense_uses_product_slot_not_list_index():
    """Regression: deleting product 0 must not shift the slot used to dispense
    the remaining products. The MQTT dispense command must carry the product's
    stable `slot` field, not its current position in the list."""
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice", price=1.0, slot=0),
        Product(sku="WATER-1", name="Water", price=1.0, slot=1),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    published = []

    class FakeMqtt:
        def register(self, *args, **kwargs):
            pass

        async def publish(self, topic, payload):
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMqtt())

    # Admin deletes the first product from the catalog via the dashboard.
    del vmc.products[0]
    assert vmc.products[0].sku == "WATER-1"

    vmc.selected_product = vmc.products[0]
    vmc.on_dispense_product()
    await asyncio.sleep(0)  # let the fire-and-forget publish task run

    topic, payload = published[-1]
    assert topic == "cmd/dispense"
    assert payload.slot == 1  # WATER-1's stable slot, not its new list index (0)
