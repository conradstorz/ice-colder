"""Unit tests for money-critical VMC flows (no broker required).

These paths were previously only covered by tests/test_integration_e2e.py,
which skips without a live MQTT broker.
"""

import asyncio
import json
import sqlite3

import pytest
from loguru import logger

from config.config_model import ConfigModel, Product
from contracts.vending_machine import (
    DispenserOutcome,
    FaultCode,
    OUTCOME_FAULTS,
    PaymentRefundCommand,
)
from controller.vmc import VMC
from services.availability import Availability
from services.event_recorder import EventRecorder
from services.health_monitor import HealthMonitor
from services.mqtt_messages import PaymentEnableCommand
from services.session_store import Credit, SessionSnapshot, SessionStore


def make_vmc(price: float = 2.50) -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="ICE-1", name="Ice Bag", price=price)]
    return VMC(config=cfg)


class FakeEventRecorder:
    def __init__(self):
        self.events: list[tuple] = []
        self.sales: list[tuple] = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        self.sales.append((sku, name, slot, price, methods))


class FakeSoldOutInventory:
    def is_available(self, sku):
        return False

    def is_tracked(self, sku):
        return True

    def decrement(self, sku, **kwargs):
        pass

    async def save_async(self):
        pass

    def get_count(self, sku):
        return 0


async def test_late_dispenser_fault_after_completed_sale_is_ignored():
    """A duplicate/late 'jam' MQTT message (QoS 0, no dedup) arriving after a
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
    await vmc._handle_mqtt_dispenser("hardware/dispenser", {"slot": 0, "state": "jam"})

    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0  # no bogus refund credited
    vmc.cancel_pending_tasks()


async def test_dispenser_jam_with_mismatched_slot_is_ignored():
    """A delayed 'jammed' report for a different slot than the active sale must
    not fault the machine or issue a refund for the wrong product."""
    vmc = make_vmc()
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.selected_product = vmc.products[0]
    vmc.machine.set_state("dispensing")
    vmc.credit_escrow = 0.0

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "jam"}
    )

    assert vmc.state == "dispensing"  # unaffected — wrong slot
    assert vmc.credit_escrow == 0.0  # no bogus refund
    assert messages == []


async def test_dispense_complete_with_mismatched_slot_is_ignored():
    """A delayed/duplicate 'complete' for a different slot than the active sale
    must not finalize the sale."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "complete"}
    )

    assert vmc.state == "dispensing"  # not finished — wrong slot
    assert vmc.selected_product is vmc.products[0]
    vmc.cancel_pending_tasks()


async def test_dispense_complete_with_matching_slot_still_completes():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    assert vmc.state == "idle"  # completed — matching slot
    assert vmc.selected_product is None
    vmc.cancel_pending_tasks()


async def test_dispense_complete_records_event_via_recorder():
    """The VMC — not the recorder listening on hardware/dispenser directly —
    is the source of truth for a 'dispense' event, since only the VMC knows
    whether the completion was actually accepted for the active sale."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    assert recorder.events == [("dispense", float(vmc.products[0].slot), None)]
    vmc.cancel_pending_tasks()


async def test_dispense_complete_with_mismatched_slot_does_not_record_event():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    other_slot = vmc.products[0].slot + 1
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": other_slot, "state": "complete"}
    )

    assert recorder.events == []
    vmc.cancel_pending_tasks()


async def test_dispense_timeout_fallback_does_not_record_event():
    """The 60s hardware-silence fallback completes the transaction without any
    hardware confirmation, so it must not record a 'dispense' event."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    recorder = FakeEventRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()
    assert vmc.state == "dispensing"

    vmc._finish_dispensing()  # simulate the 60s hardware-silence fallback firing
    assert vmc.state == "idle"
    assert recorder.events == []
    vmc.cancel_pending_tasks()


async def test_dispense_complete_records_sale_durably_before_fsm_returns_to_idle(
    tmp_path,
):
    """The sale row must be committed to the real database before the FSM
    leaves 'dispensing' -- not merely by the time this test's own
    assertions happen to run afterward. A recorder stub captures
    `vmc.state` from *inside* `record_sale`, at the instant the durable
    write is invoked via `asyncio.to_thread` (while the coroutine awaiting
    it has not yet resumed) -- that is the ordering property under test,
    not mere co-occurrence.
    """
    real_recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())

    captured_state = {}

    class OrderCapturingRecorder:
        def record(self, event_type, value=1.0, metadata=None):
            pass  # the "dispense" event itself isn't under test here

        def record_sale(self, sku, name, slot, price, methods, ts=None):
            captured_state["state"] = vmc.state
            return real_recorder.record_sale(sku, name, slot, price, methods, ts=ts)

    vmc.set_event_recorder(OrderCapturingRecorder())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    # Real deposits through deposit_funds, not a direct credit_escrow
    # assignment -- bypassing the ledger trips the divergence guard and
    # _consume_credits_fifo returns {"unknown": price}, which would prove
    # nothing about the FIFO split under test here.
    vmc.deposit_funds(1.50, payment_method="cash_bill")
    vmc.deposit_funds(1.00, payment_method="card")

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.pending_sale_shares == {"cash_bill": 1.50, "card": 1.00}

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    # The write happened while the FSM was still in 'dispensing' -- proven
    # from inside the call, not reconstructed afterward.
    assert captured_state["state"] == "dispensing"
    assert vmc.state == "idle"  # ...and only *afterward* did it return to idle
    assert vmc.pending_sale_shares is None  # cleared on the success path

    with sqlite3.connect(str(tmp_path / "events.db")) as conn:
        rows = conn.execute(
            "SELECT sku, name, slot, price, methods FROM sales"
        ).fetchall()
    assert len(rows) == 1  # exactly one row
    sku, name, slot, price, methods_json = rows[0]
    assert sku == "ICE-1"
    assert name == "Ice Bag"
    assert slot == vmc.products[0].slot
    assert price == pytest.approx(2.50)
    assert json.loads(methods_json) == {"cash_bill": 1.50, "card": 1.00}
    vmc.cancel_pending_tasks()


async def test_dispense_complete_sale_record_failure_raises_data_101_but_completes_vend():
    """record_sale already journals the row and re-raises on failure
    (services/event_recorder.py) -- the VMC must not journal it a second
    time, only catch the exception, raise the alert-class DATA-101, and let
    the vend finish regardless. A storage problem must never fail the vend
    or stop the machine."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())

    class FailingRecorder:
        def __init__(self):
            self.record_sale_calls = 0

        def record(self, event_type, value=1.0, metadata=None):
            pass

        def record_sale(self, sku, name, slot, price, methods, ts=None):
            # Simulates record_sale's own contract: it journals internally
            # and re-raises -- the VMC is never asked to journal on top.
            self.record_sale_calls += 1
            raise sqlite3.OperationalError("disk I/O error")

    recorder = FailingRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(2.50, payment_method="cash_coin")

    vmc._process_payment()
    assert vmc.state == "dispensing"

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    # Proves the failure branch was genuinely entered, not skipped.
    assert recorder.record_sale_calls == 1
    assert vmc.state == "idle"  # the vend still completed
    assert vmc.selected_product is None
    assert vmc.pending_sale_shares is None  # cleared even on the failure path
    assert "DATA-101" in {f["code"] for f in vmc.active_faults()}
    vmc.cancel_pending_tasks()


async def test_dispense_complete_records_price_from_shares_not_live_catalog_price(
    tmp_path,
):
    """`selected_product` is the *live* catalog ``Product`` object -- the
    same one `services/config_store.update_product` mutates in place. If an
    operator edits the price while a sale is mid-dispense, `product.price`
    at record time reflects the *new* price, not what the customer actually
    paid. The stored row must reflect `pending_sale_shares` (the money
    actually deducted), matching `methods`, not a live catalog re-read.
    """
    real_recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())

    class SaleOnlyRecorder:
        """Forwards only record_sale to the real recorder -- mirrors
        test_dispense_complete_records_sale_durably_before_fsm_returns_to_idle's
        OrderCapturingRecorder above. The "dispense" event this test doesn't
        care about is otherwise queued to the real recorder's background
        writer thread, which can race record_sale's own fresh WAL
        connection for the same db file; that race is a pre-existing,
        unrelated timing issue outside the scope of this fix.
        """

        def record(self, event_type, value=1.0, metadata=None):
            pass

        def record_sale(self, sku, name, slot, price, methods, ts=None):
            return real_recorder.record_sale(sku, name, slot, price, methods, ts=ts)

    vmc.set_event_recorder(SaleOnlyRecorder())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(2.50, payment_method="cash_bill")

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.pending_sale_shares == {"cash_bill": 2.50}

    # Operator edits the catalog price mid-flight, in place -- exactly what
    # services/config_store.update_product does to the same live object.
    vmc.selected_product.price = 9.99

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )
    assert vmc.state == "idle"

    with sqlite3.connect(str(tmp_path / "events.db")) as conn:
        rows = conn.execute("SELECT price, methods FROM sales").fetchall()
    assert len(rows) == 1
    price, methods_json = rows[0]
    methods = json.loads(methods_json)
    assert methods == {"cash_bill": 2.50}
    # The row must record what was actually charged, not the edited price.
    assert price == pytest.approx(2.50)
    vmc.cancel_pending_tasks()


async def test_vend_failed_after_deduction_writes_no_sale_row_and_restores_credits(
    tmp_path,
):
    """A failed vend must never turn into a sale row: the money already
    deducted from escrow is restored as credits, not spent. Checked against
    the real database through a second connection -- not a log line -- so
    this guards against double-counting a failed vend."""
    real_recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    vmc = make_vmc2()  # two products: WATER-1 stays sellable after ICE-1 locks
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_event_recorder(real_recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]  # ICE-1, price 2.50
    vmc.deposit_funds(2.50, payment_method="cash_bill")

    vmc._process_payment()
    assert vmc.state == "dispensing"
    # Proves the deduction genuinely happened before the failure.
    assert vmc.pending_sale_shares == {"cash_bill": 2.50}

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": vmc.products[0].slot, "state": "jam"}
    )

    assert vmc.state == "interacting_with_user"  # vend_failed, WATER-1 still sellable
    assert vmc.pending_sale_shares is None
    assert vmc.credit_escrow == 2.50  # restored, not spent
    assert len(vmc.escrow_credits) == 1
    assert vmc.escrow_credits[0].method == "cash_bill"
    assert vmc.escrow_credits[0].amount == 2.50

    with sqlite3.connect(str(tmp_path / "events.db")) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    assert count == 0  # no sale row for a vend that never completed
    vmc.cancel_pending_tasks()


async def test_vend_failed_restores_shares_total_not_live_catalog_price(tmp_path):
    """Same live-object hazard as the record_sale test above, on the other
    branch: `on_vend_failed` must restore exactly what `pending_sale_shares`
    says was deducted, not re-read `product.price` after an operator edited
    it mid-flight. Otherwise escrow is re-credited a different total than
    was taken, and the failure event/log/customer message all report the
    wrong amount too.
    """
    real_recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
    vmc = make_vmc2()  # two products: WATER-1 stays sellable after ICE-1 locks
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_event_recorder(real_recorder)
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]  # ICE-1, price 2.50
    vmc.deposit_funds(2.50, payment_method="cash_bill")

    vmc._process_payment()
    assert vmc.state == "dispensing"
    assert vmc.pending_sale_shares == {"cash_bill": 2.50}

    # Operator edits the catalog price mid-flight, in place.
    vmc.selected_product.price = 9.99

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": vmc.products[0].slot, "state": "jam"}
    )

    assert vmc.state == "interacting_with_user"
    assert vmc.pending_sale_shares is None
    # Restored total must match what was actually deducted, not the edited
    # catalog price.
    assert vmc.credit_escrow == pytest.approx(2.50)
    assert len(vmc.escrow_credits) == 1
    assert vmc.escrow_credits[0].method == "cash_bill"
    assert vmc.escrow_credits[0].amount == pytest.approx(2.50)

    # The customer message must also report what was actually taken, not
    # the edited catalog price.
    assert any("$2.50" in m for m in messages)
    assert not any("$9.99" in m for m in messages)

    real_recorder.flush()
    with sqlite3.connect(str(tmp_path / "events.db")) as conn:
        row = conn.execute(
            "SELECT value FROM events WHERE event_type = 'vend_failed'"
        ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(2.50)
    vmc.cancel_pending_tasks()


async def test_pay_104_snapshot_exposes_pending_sale_after_crash_mid_dispense(
    tmp_path,
):
    """Simulates a real crash right after the dispense command is sent but
    before the FSM ever hears back: the live `_process_payment` path (Task
    4) already persists a 'dispensing' snapshot carrying
    `pending_sale_shares`. This task's job is only to make sure that data
    survives to a reboot and is exposed on the resulting PAY-104 -- Task 14
    builds the record/discard recovery routes on top of it, not this task.

    Driven entirely through the real deposit/process-payment/reboot
    pipeline (not a hand-built SessionSnapshot), so a regression in
    `_snapshot()` dropping `pending_sale_shares` would be caught here.
    """
    store_path = tmp_path / "session.json"
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_session_store(SessionStore(store_path))
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(2.00, payment_method="cash_bill")
    vmc.deposit_funds(0.50, payment_method="card")

    vmc._process_payment()  # persists the 'dispensing' snapshot (Task 4)
    assert vmc.state == "dispensing"
    await vmc.drain_persistence()  # the save is fire-and-forget -- wait for it
    vmc.cancel_pending_tasks()  # simulate the crash: nothing else ever runs

    # A fresh VMC instance boots against the same evidence file.
    vmc2 = make_vmc(price=2.50)
    vmc2.attach_to_loop(asyncio.get_running_loop())
    vmc2.set_session_store(SessionStore(store_path))  # loads open snap -> PAY-104

    assert "PAY-104" in {f["code"] for f in vmc2.active_faults()}
    reloaded = SessionStore(store_path).load()
    assert reloaded.pending_sale_shares == {"cash_bill": 2.00, "card": 0.50}
    assert reloaded.selected_sku == "ICE-1"
    assert reloaded.dispense_slot == vmc.products[0].slot
    vmc2.cancel_pending_tasks()


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


async def test_insufficient_funds_prompt_is_not_an_error_log():
    """A customer who hasn't inserted enough yet is routine, not an ERROR."""
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 0.50
    records: list[tuple[str, str]] = []
    handle = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    try:
        vmc._process_payment()
    finally:
        logger.remove(handle)
        vmc.cancel_pending_tasks()  # drop the 5 s retry _process_payment scheduled
    prompts = [lvl for lvl, msg in records if "Insufficient funds" in msg]
    assert "INFO" in prompts
    assert "ERROR" not in prompts


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

        async def publish(self, topic, payload, **kwargs):
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


def make_vmc2() -> VMC:
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0),
        Product(sku="WATER-1", name="Water", price=1.00, slot=1),
    ]
    return VMC(config=cfg)


class TestFaultRegistry:
    async def test_lockout_fault_locks_product_and_alerts(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)

        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1", outcome="timeout")
        await asyncio.sleep(0)

        assert vmc._lockouts == {"ICE-1": FaultCode.ICE_301}
        assert ("lockout_set", 1.0, {"code": "ICE-301", "sku": "ICE-1"}) in rec.events
        assert "ICE-301:ICE-1" in hm._fired_alerts
        faults = hm.get_summary()["active_faults"]
        assert faults[0]["code"] == "ICE-301" and faults[0]["product"] == "Ice Bag"

    async def test_vend_failed_severity_does_not_lock(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_102, sku="ICE-1")
        assert vmc._lockouts == {}
        assert vmc.active_faults() == []

    async def test_machine_scope_fault_keyed_by_code(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._raise_fault(FaultCode.PAY_103)
        faults = vmc.active_faults()
        assert faults == [
            {
                "key": "PAY-103",
                "sku": None,
                "product": None,
                "code": "PAY-103",
                "severity": "warning",
                "scope": "machine",
                "description": "Refund not confirmed by payment gateway; needs reconciliation",
            }
        ]
        assert vmc.clear_fault("PAY-103") is True
        assert vmc.active_faults() == []

    async def test_select_locked_product_is_refused(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")

        vmc.select_product(0)

        assert vmc.state == "idle"
        assert vmc.selected_product is None
        assert any("ICE-301" in m for m in messages)

    async def test_sellable_products_excludes_locked(self):
        vmc = make_vmc2()
        vmc._raise_fault(FaultCode.ICE_401, sku="ICE-1")
        assert [p.sku for p in vmc._sellable_products()] == ["WATER-1"]

    async def test_clear_fault_records_and_rearms_alert(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")
        await asyncio.sleep(0)

        assert vmc.clear_fault("ICE-1") is True
        assert vmc._lockouts == {}
        assert "ICE-301:ICE-1" not in hm._fired_alerts
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-301", "sku": "ICE-1", "by": "admin"},
        ) in rec.events
        assert hm.get_summary()["active_faults"] == []
        assert vmc.clear_fault("ICE-1") is False

    async def test_bin_half_full_auto_clears_ice_101(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc._raise_fault(FaultCode.ICE_101, sku="ICE-1")
        vmc._raise_fault(FaultCode.ICE_301, sku="WATER-1")

        await vmc._handle_mqtt_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
        )

        assert vmc._lockouts == {"WATER-1": FaultCode.ICE_301}
        assert (
            "lockout_cleared",
            1.0,
            {"code": "ICE-101", "sku": "ICE-1", "by": "auto"},
        ) in rec.events

    def test_hardware_io_handler_is_registered(self):
        vmc = make_vmc2()

        class FakeClient:
            def __init__(self):
                self.topics = []

            def register(self, topic, handler):
                self.topics.append(topic)

        client = FakeClient()
        vmc.set_mqtt_client(client)
        assert "hardware/io/+" in client.topics


def _start_dispensing(vmc: VMC, index: int = 0):
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[index]
    vmc.credit_escrow = vmc.products[index].price
    vmc._process_payment()
    assert vmc.state == "dispensing"


class TestVendOutcomes:
    @pytest.mark.parametrize("outcome", ["timeout", "jam", "bin_empty", "error"])
    async def test_failure_outcome_restores_credit_and_records(self, outcome):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        published: list[tuple[str, object]] = []

        class FakeClient:
            def register(self, *_):
                pass

            async def publish(self, topic, payload, **kwargs):
                published.append((topic, payload))

        vmc.set_mqtt_client(FakeClient())
        _start_dispensing(vmc, 0)
        price = vmc.products[0].price

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": outcome}
        )
        await asyncio.sleep(0)

        code = OUTCOME_FAULTS[DispenserOutcome(outcome)]
        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == price
        assert vmc.selected_product is None
        assert (
            "vend_failed",
            price,
            {"code": code.value, "sku": "ICE-1", "outcome": outcome},
        ) in rec.events
        assert not any(t == "cmd/payment/refund" for t, _ in published)
        assert vmc._lockouts == {"ICE-1": code}
        assert vmc._dispense_timeout_task is None

    async def test_intermediate_state_does_not_end_sale(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "fill_complete"}
        )
        assert vmc.state == "dispensing"

    async def test_dispense_timeout_is_a_failed_vend(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc._dispense_timeout_seconds = 0.01
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)

        await asyncio.sleep(0.05)

        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == 2.50
        assert (
            "vend_failed",
            2.50,
            {"code": "PAY-102", "sku": "ICE-1", "outcome": "no_report"},
        ) in rec.events
        assert vmc._lockouts == {}  # PAY-102 is vend_failed severity: no lockout
        assert not any(e[0] == "dispense" for e in rec.events)

    async def test_timeout_seconds_come_from_config(self):
        cfg = ConfigModel()
        cfg.physical.dispense_timeout_seconds = 45.0
        cfg.physical.products = [Product(sku="ICE-1", name="Ice", price=1.0)]
        vmc = VMC(config=cfg)
        assert vmc._dispense_timeout_seconds == 45.0

    async def test_complete_after_failure_is_ignored(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        _start_dispensing(vmc, 0)
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "timeout"}
        )
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        assert vmc.state == "interacting_with_user"
        assert not any(e[0] == "dispense" for e in rec.events)


class TestCreditLedger:
    """The FIFO escrow credit ledger (§1.1): deposit -> deduct -> restore/refund."""

    async def test_fifo_worked_example_splits_and_leaves_remainder(self):
        """The spec's own acceptance example: $2.00 cash then $1.00 card,
        a $2.50 sale, must yield {"cash": 2.00, "card": 0.50} and leave
        exactly one $0.50 card credit — not just a total that happens to
        add up."""
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.deposit_funds(2.00, payment_method="cash_bill")
        vmc.deposit_funds(1.00, payment_method="card")

        vmc._process_payment()

        # Confirms the deduction branch (credit_escrow >= price) actually ran,
        # rather than the insufficient-funds branch silently passing.
        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"cash_bill": 2.00, "card": 0.50}
        assert vmc.credit_escrow == 0.50
        assert len(vmc.escrow_credits) == 1
        assert vmc.escrow_credits[0].method == "card"
        assert vmc.escrow_credits[0].amount == 0.50
        vmc.cancel_pending_tasks()

    async def test_exact_match_consumes_one_credit_entirely(self):
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.deposit_funds(2.50, payment_method="cash_coin")

        vmc._process_payment()

        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"cash_coin": 2.50}
        assert vmc.escrow_credits == []
        assert vmc.credit_escrow == 0.0
        vmc.cancel_pending_tasks()

    async def test_sale_spanning_three_credits(self):
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.deposit_funds(1.00, payment_method="cash_coin")
        vmc.deposit_funds(1.00, payment_method="cash_coin")
        vmc.deposit_funds(1.00, payment_method="card")

        vmc._process_payment()

        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"cash_coin": 2.00, "card": 0.50}
        assert len(vmc.escrow_credits) == 1
        assert vmc.escrow_credits[0].method == "card"
        assert vmc.escrow_credits[0].amount == 0.50
        vmc.cancel_pending_tasks()

    async def test_vend_failed_restores_separate_credits_with_original_methods(self):
        """vend_failed must re-credit the exact per-method shares that were
        consumed, as separate Credits — not one blob under the default/last
        payment method. This is the property that stops the ledger
        laundering cash into card."""
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.deposit_funds(2.00, payment_method="cash_bill")
        vmc.deposit_funds(0.50, payment_method="card")
        vmc._process_payment()
        assert vmc.state == "dispensing"  # sale actually in flight
        assert vmc.pending_sale_shares == {"cash_bill": 2.00, "card": 0.50}
        assert vmc.escrow_credits == []  # both credits fully consumed

        vmc.vend_failed(code=FaultCode.PAY_102, outcome="no_report")

        assert vmc.state == "interacting_with_user"
        assert vmc.credit_escrow == 2.50
        assert vmc.pending_sale_shares is None
        assert len(vmc.escrow_credits) == 2
        assert vmc.escrow_credits[0].method == "cash_bill"
        assert vmc.escrow_credits[0].amount == 2.00
        assert vmc.escrow_credits[1].method == "card"
        assert vmc.escrow_credits[1].amount == 0.50
        vmc.cancel_pending_tasks()

    async def test_rejected_deposit_appends_no_credit(self):
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())

        vmc.deposit_funds(0.0, payment_method="cash_coin")
        vmc.deposit_funds(-1.0, payment_method="cash_coin")

        assert vmc.escrow_credits == []
        assert vmc.credit_escrow == 0.0

    async def test_divergence_guard_books_unknown_and_warns(self):
        """credit_escrow mutated directly (bypassing deposit_funds, as many
        pre-existing tests in this file do) leaves escrow_credits empty
        while credit_escrow is nonzero. _consume_credits_fifo must not
        guess a method in that case: it books the whole price to 'unknown'
        and logs a warning, rather than attributing real money to the
        wrong (or no) method."""
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.credit_escrow = 5.00  # escrow_credits stays [] -> diverges

        records: list[tuple[str, str]] = []
        handle = logger.add(
            lambda m: records.append((m.record["level"].name, m.record["message"])),
            level="DEBUG",
            format="{message}",
        )
        try:
            vmc._process_payment()
        finally:
            logger.remove(handle)

        assert vmc.state == "dispensing"  # deduction branch ran
        assert vmc.pending_sale_shares == {"unknown": 2.50}
        assert vmc.credit_escrow == 2.50
        assert any(lvl == "WARNING" and "diverged" in msg for lvl, msg in records)
        vmc.cancel_pending_tasks()

    async def test_float_boundary_tolerance(self):
        """CREDIT_TOLERANCE (0.005, half a cent) is used in two places; both
        boundaries are tested here.

        1. The leftover-credit decision: a genuine one-cent overshoot is
           real money and must survive as its own credit — the tolerance
           must never be generous enough to eat an actual cent.
        2. The divergence guard: escrow_credits and credit_escrow rounded to
           the cent agreeing exactly is trusted; even the smallest real
           disagreement once both sides are cent-quantized — one cent — must
           trip the guard rather than silently attribute real money to
           whatever methods happen to be sitting in an untrustworthy list.
        """
        # (1) $2.51 deposited, $2.50 charged -> a real $0.01 remains.
        vmc = make_vmc(price=2.50)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")
        vmc.selected_product = vmc.products[0]
        vmc.deposit_funds(2.51, payment_method="cash_coin")

        vmc._process_payment()

        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"cash_coin": 2.50}
        assert len(vmc.escrow_credits) == 1
        assert vmc.escrow_credits[0].amount == 0.01
        vmc.cancel_pending_tasks()

        # (2a) Ledger and total agree exactly -> trusted, FIFO shares returned.
        vmc2 = make_vmc(price=1.00)
        vmc2.attach_to_loop(asyncio.get_running_loop())
        vmc2.machine.set_state("interacting_with_user")
        vmc2.selected_product = vmc2.products[0]
        vmc2.escrow_credits = [Credit(method="cash_coin", amount=1.00, ts=0.0)]
        vmc2.credit_escrow = 1.00

        vmc2._process_payment()

        assert vmc2.state == "dispensing"
        assert vmc2.pending_sale_shares == {"cash_coin": 1.00}
        vmc2.cancel_pending_tasks()

        # (2b) One cent off -> the guard trips; escrow_credits is left
        # untouched (not consumed, not merged) and the share is "unknown".
        vmc3 = make_vmc(price=1.00)
        vmc3.attach_to_loop(asyncio.get_running_loop())
        vmc3.machine.set_state("interacting_with_user")
        vmc3.selected_product = vmc3.products[0]
        vmc3.escrow_credits = [Credit(method="cash_coin", amount=1.00, ts=0.0)]
        vmc3.credit_escrow = 1.01

        vmc3._process_payment()

        assert vmc3.state == "dispensing"
        assert vmc3.pending_sale_shares == {"unknown": 1.00}
        assert vmc3.escrow_credits == [Credit(method="cash_coin", amount=1.00, ts=0.0)]
        vmc3.cancel_pending_tasks()


class RecordingClient:
    def __init__(self):
        self.published: list[tuple[str, object]] = []

    def register(self, *_):
        pass

    async def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload))

    def refund_commands(self) -> list[PaymentRefundCommand]:
        return [p for t, p in self.published if t == "cmd/payment/refund"]


class TestRefunds:
    async def test_request_refund_publishes_command_and_zeroes_escrow(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc.credit_escrow = 1.75

        vmc.request_refund(reason="session_timeout")
        await asyncio.sleep(0)

        cmds = client.refund_commands()
        assert len(cmds) == 1
        assert cmds[0].amount == 1.75
        assert cmds[0].reason == "session_timeout"
        assert vmc.credit_escrow == 0.0
        assert cmds[0].request_id in vmc._pending_refunds
        # A refund isn't real until the gateway acks it — don't tell the
        # customer it's "issued" before that happens.
        assert "requested" in messages[-1]
        assert "issued" not in messages[-1]

    async def test_refund_clears_credit_list_as_well_as_total(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        vmc.deposit_funds(1.00, payment_method="cash_coin")
        vmc.deposit_funds(0.75, payment_method="card")
        assert len(vmc.escrow_credits) == 2  # confirms deposit_funds populated it

        vmc.request_refund(reason="cancel")

        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []

    async def test_ack_ok_records_refund(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc.credit_escrow = 2.0
        vmc.request_refund(reason="cancel")
        await asyncio.sleep(0)
        rid = client.refund_commands()[0].request_id

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "ok", "amount_returned": 2.0},
        )

        assert rid not in vmc._pending_refunds
        assert ("refund", 2.0, {"request_id": rid, "reason": "cancel"}) in rec.events
        # Only now, after the ack, may the customer be told it's issued.
        assert "issued" in messages[-1]
        assert "$2.00" in messages[-1]

    async def test_ack_failed_retries_once_then_pay_103(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc.credit_escrow = 2.0
        vmc.request_refund(reason="cancel")
        await asyncio.sleep(0)
        rid = client.refund_commands()[0].request_id

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "failed", "detail": "changer_empty"},
        )
        await asyncio.sleep(0)
        assert [c.request_id for c in client.refund_commands()] == [rid, rid]
        assert rid in vmc._pending_refunds
        # Still just a retry in flight — no promise made either way yet.
        assert "issued" not in messages[-1]

        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "failed", "detail": "changer_empty"},
        )
        await asyncio.sleep(0)

        assert rid not in vmc._pending_refunds
        assert len(client.refund_commands()) == 2
        assert (
            "refund_failed",
            2.0,
            {"request_id": rid, "reason": "cancel", "detail": "changer_empty"},
        ) in rec.events
        assert "PAY-103" in [f["code"] for f in vmc.active_faults()]
        # Final failure must tell the customer to contact support, never
        # that the refund was issued.
        assert "contact support" in messages[-1]
        assert rid[:8] in messages[-1]
        assert "issued" not in messages[-1]

    async def test_no_ack_deadline_retries_then_pay_103(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.REFUND_ACK_TIMEOUT = 0.01
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        vmc.credit_escrow = 3.0
        vmc.request_refund(reason="error")

        await asyncio.sleep(0.1)

        assert len(client.refund_commands()) == 2
        assert vmc._pending_refunds == {}
        assert any(
            e[0] == "refund_failed" and e[2]["detail"] == "ack_timeout"
            for e in rec.events
        )
        assert "PAY-103" in [f["code"] for f in vmc.active_faults()]

    async def test_unknown_request_id_ack_is_ignored(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": "x" * 32, "status": "ok", "amount_returned": 1.0},
        )
        assert rec.events == []

    async def test_on_error_pays_out_via_refund_command(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc.machine.set_state("interacting_with_user")
        vmc.credit_escrow = 1.25

        vmc.error_occurred()
        await asyncio.sleep(0)

        assert vmc.state == "error"
        assert vmc.credit_escrow == 0.0
        cmds = client.refund_commands()
        assert len(cmds) == 1 and cmds[0].amount == 1.25 and cmds[0].reason == "error"
        # The refund is only requested here, not confirmed — the final
        # customer-facing message must not claim it has already happened.
        assert "requested" in messages[-1]
        assert "refunded" not in messages[-1]

    async def test_on_error_without_credit_says_contact_support_only(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        messages: list[str] = []
        vmc.set_message_callback(messages.append)
        vmc.machine.set_state("interacting_with_user")
        vmc.credit_escrow = 0.0

        vmc.error_occurred()
        await asyncio.sleep(0)

        assert vmc.state == "error"
        assert client.refund_commands() == []
        assert messages[-1] == "An error has occurred. Please contact support."

    async def test_all_products_locked_refunds_and_idles(self):
        vmc = make_vmc()  # single product
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        _start_dispensing(vmc, 0)

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "jam"}
        )
        await asyncio.sleep(0)

        assert vmc.state == "idle"
        assert vmc.credit_escrow == 0.0
        refunds = client.refund_commands()
        assert len(refunds) == 1
        assert refunds[0].amount == 2.50
        assert refunds[0].reason == "ICE-401"

    def test_refund_ack_handler_is_registered(self):
        vmc = make_vmc2()
        client = RecordingClient()
        topics = []
        client.register = lambda topic, handler: topics.append(topic)
        vmc.set_mqtt_client(client)
        assert "cmd/payment/refund/ack" in topics


class TestFireAndForget:
    async def test_failing_background_task_is_logged_not_lost(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        seen: list[str] = []
        handle = logger.add(
            lambda m: seen.append(str(m)), level="ERROR", format="{message}"
        )
        try:

            async def boom():
                raise RuntimeError("publish exploded")

            vmc._fire_and_forget(boom())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            logger.remove(handle)
        assert any("publish exploded" in s for s in seen)

    async def test_background_task_is_tracked_until_done(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)

        vmc._fire_and_forget(slow())
        await started.wait()
        assert any(not t.done() for t in vmc._pending_tasks)
        vmc.cancel_pending_tasks()
        await asyncio.sleep(0)
        assert all(t.done() for t in vmc._pending_tasks) or vmc._pending_tasks == []


def _wired_vmc(products=None):
    cfg = ConfigModel()
    cfg.physical.products = products or [
        Product(sku="ICE-1", name="Ice Bag", price=2.5, kind="ice"),
        Product(sku="WTR-1", name="Water", price=1.0, kind="water"),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    monitor = HealthMonitor()
    vmc.set_health_monitor(monitor)
    avail = Availability()
    vmc.set_availability(avail)
    published: list = []

    class FakeMQTT:
        def register(self, *a, **k):
            pass

        async def publish(self, topic, payload, qos=1, retain=False):
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMQTT())
    return vmc, monitor, avail, published


def _all_alive(monitor: HealthMonitor, vmc: VMC):
    for name in ("vending", "mdb", "ice_maker"):
        monitor.record_heartbeat(name, {"uptime_seconds": 1})
    vmc.on_mqtt_connection(True)


async def _enables(published) -> list[bool]:
    await asyncio.sleep(0)
    return [
        p.accept
        for t, p in published
        if t == "cmd/payment/enable" and isinstance(p, PaymentEnableCommand)
    ]


async def test_vending_heartbeat_loss_raises_com_101_without_disabling_payment():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    assert avail.payment_enabled is True

    monitor.mark_offline("vending")
    codes = {f["code"] for f in vmc.active_faults()}
    assert "COM-101" in codes
    assert avail.payment_enabled is True
    assert avail.sale_available("ice")[0] is False
    assert "vending_alive" in avail.sale_available("ice")[1]

    monitor.record_heartbeat("vending", {"uptime_seconds": 5})
    assert "COM-101" not in {f["code"] for f in vmc.active_faults()}
    assert avail.sale_available("ice")[0] is True
    vmc.cancel_pending_tasks()


async def test_ice_maker_loss_is_com_102_and_only_ice_blocked():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    monitor.mark_offline("ice_maker")
    assert "COM-102" in {f["code"] for f in vmc.active_faults()}
    assert avail.sale_available("ice")[0] is False
    assert avail.payment_enabled is True
    vmc.cancel_pending_tasks()


async def test_mdb_loss_is_pay_101_and_blocks_sales_not_payment():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    monitor.mark_offline("mdb")
    assert "PAY-101" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    assert "payment_alive" in avail.sale_available("ice")[1]
    vmc.cancel_pending_tasks()


async def test_mqtt_disconnect_is_com_103_and_reconnect_republishes():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    vmc.on_mqtt_connection(False)
    assert "COM-103" in {f["code"] for f in vmc.active_faults()}
    before = len(await _enables(published))
    vmc.on_mqtt_connection(True)
    assert "COM-103" not in {f["code"] for f in vmc.active_faults()}
    assert len(await _enables(published)) == before + 1
    vmc.cancel_pending_tasks()


async def test_payment_status_error_feeds_availability():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    await vmc._handle_mqtt_payment_status(
        "payment/status", {"device": "card_reader", "state": "error"}
    )
    assert "payment_devices_ready" in avail.sale_available("ice")[1]
    vmc.cancel_pending_tasks()


async def test_select_product_refused_when_kind_unavailable_names_reason():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    monitor.mark_offline("ice_maker")
    messages = []
    vmc.set_message_callback(messages.append)
    vmc.select_product(0)  # ICE-1
    assert vmc.state == "idle"
    assert vmc.selected_product is None
    assert "ice_maker_alive" in messages[-1]
    vmc.cancel_pending_tasks()


async def test_deposit_while_disabled_is_escrowed_and_logged():
    vmc, monitor, avail, _ = _wired_vmc()
    vmc.deposit_funds(1.0, payment_method="cash_coin")
    assert vmc.credit_escrow == 1.0
    vmc.cancel_pending_tasks()


def _boot_with(tmp_path, snap):
    store = SessionStore(tmp_path / "session.json")
    if snap is not None:
        store.save(snap)
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)
    return vmc, avail, store


async def test_pay_104_on_boot_leaves_payment_enabled(tmp_path):
    rec = FakeEventRecorder()
    vmc, avail, store = _boot_with(tmp_path, None)
    vmc.set_event_recorder(rec)
    store.save(SessionSnapshot(state="interacting_with_user", credit_escrow=1.25))
    vmc.set_session_store(store)

    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    assert avail.payment_blocking_reasons() == []
    assert store.load() is not None  # evidence kept until an admin clears it
    vmc.cancel_pending_tasks()


async def test_pay_104_is_reported_as_a_warning():
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    vmc._raise_fault(FaultCode.PAY_104, outcome="test")
    fault = next(f for f in vmc.active_faults() if f["code"] == "PAY-104")
    assert fault["severity"] == "warning"
    assert fault["scope"] == "machine"
    assert avail.payment_enabled is True
    vmc.cancel_pending_tasks()


async def test_select_product_refused_while_vending_offline_but_payment_stays_on():
    """Immediately after a refused selection the escrow is still held — but it
    must not stay stranded forever. See
    test_deposit_while_idle_and_vending_offline_is_refunded_on_timeout below
    for the guarantee that the session timeout eventually refunds it.
    """
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    monitor.mark_offline("vending")

    assert avail.payment_enabled is True
    vmc.deposit_funds(2.00)
    vmc.select_product(0)
    assert vmc.selected_product is None
    assert vmc.credit_escrow == 2.00
    assert vmc.state == "idle"
    vmc.cancel_pending_tasks()


async def test_deposit_while_idle_arms_session_timeout():
    """A deposit before any selection is a legitimate entry point (see
    on_start_interaction's own "insert funds or select a product" message),
    so it must arm the safety-net timer even though the FSM stays idle.
    """
    vmc, monitor, avail, _ = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")

    assert vmc.state == "idle"
    assert vmc._session_timeout_task is None
    vmc.deposit_funds(2.00)

    assert vmc._session_timeout_task is not None
    vmc.cancel_pending_tasks()


async def test_deposit_while_idle_and_vending_offline_is_refunded_on_timeout():
    """Regression test for the Critical finding: money deposited while idle
    (vending subsystem offline, so the only selection attempt is refused)
    must still be refunded when the session times out — never stranded
    silently forever.
    """
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    monitor.mark_offline("vending")
    vmc._session_timeout_seconds = 0.05

    assert avail.payment_enabled is True
    vmc.deposit_funds(2.00)
    vmc.select_product(0)
    assert vmc.selected_product is None
    assert vmc.credit_escrow == 2.00
    assert vmc.state == "idle"

    await asyncio.sleep(0.3)

    refund_cmds = [p for t, p in published if t == "cmd/payment/refund"]
    assert len(refund_cmds) == 1
    assert refund_cmds[0].reason == "session_timeout"
    assert vmc.credit_escrow == 0.0
    assert vmc.state == "idle"
    vmc.cancel_pending_tasks()


async def test_expire_session_is_a_noop_while_dispensing():
    """A vend already in flight must never be refunded out from under the
    customer just because a stale/late timeout callback fires."""
    vmc = make_vmc2()
    vmc.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    vmc.set_mqtt_client(client)
    _start_dispensing(vmc, 0)
    assert vmc.state == "dispensing"
    escrow_before = vmc.credit_escrow

    vmc._expire_session()

    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == escrow_before
    assert client.refund_commands() == []
    vmc.cancel_pending_tasks()


async def test_deposit_after_on_error_refund_is_refunded_on_timeout():
    """Regression test: on_error refunds whatever escrow existed when the
    error was raised, but deposit_funds arms the session timer on every
    deposit regardless of state. A credit that arrives while the machine is
    still parked in `error` (awaiting an admin reset_state) must not be
    silently stranded when that timer fires.
    """
    vmc = make_vmc2()
    vmc.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    vmc.set_mqtt_client(client)

    vmc.credit_escrow = 0.0
    vmc.error_occurred()
    await asyncio.sleep(0)
    assert vmc.state == "error"
    assert client.refund_commands() == []  # nothing to refund yet

    vmc.deposit_funds(1.50)
    assert vmc.credit_escrow == 1.50
    assert vmc._session_timeout_task is not None

    vmc._expire_session()
    await asyncio.sleep(0)

    cmds = client.refund_commands()
    assert len(cmds) == 1
    assert cmds[0].amount == 1.50
    assert cmds[0].reason == "session_timeout"
    assert vmc.credit_escrow == 0.0
    assert vmc.state == "error"  # still needs an admin reset_state
    vmc.cancel_pending_tasks()


async def test_hazard_fault_still_disables_payment():
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    vmc._raise_fault(FaultCode.WTR_104, outcome="leak")
    assert avail.payment_enabled is False
    assert avail.payment_blocking_reasons() == ["no_critical_fault"]
    assert (await _enables(published))[-1] is False
    vmc.cancel_pending_tasks()


async def test_clean_boot_raises_nothing(tmp_path):
    vmc, avail, _ = _boot_with(tmp_path, None)
    assert vmc.active_faults() == []
    vmc.cancel_pending_tasks()


async def test_boot_with_escrow_raises_pay_104_without_blocking(tmp_path):
    rec = FakeEventRecorder()
    vmc, avail, store = _boot_with(tmp_path, None)
    vmc.set_event_recorder(rec)
    store.save(SessionSnapshot(state="interacting_with_user", credit_escrow=1.25))
    vmc.set_session_store(store)
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "fail"
    assert any(
        e[0] == "session_uncertain" and e[2]["credit_escrow"] == 1.25
        for e in rec.events
    )
    assert store.load() is not None  # kept as evidence until cleared
    vmc.cancel_pending_tasks()


async def test_boot_mid_dispense_raises_pay_104(tmp_path):
    vmc, avail, _ = _boot_with(
        tmp_path,
        SessionSnapshot(
            state="dispensing", credit_escrow=0.0, selected_sku="ICE-1", dispense_slot=0
        ),
    )
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    vmc.cancel_pending_tasks()


async def test_boot_with_corrupt_file_raises_pay_104(tmp_path):
    (tmp_path / "session.json").write_text("garbage", encoding="utf-8")
    vmc, avail, _ = _boot_with(tmp_path, None)
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    vmc.cancel_pending_tasks()


async def test_clearing_pay_104_removes_file_and_reenables(tmp_path):
    vmc, avail, store = _boot_with(
        tmp_path, SessionSnapshot(state="interacting_with_user", credit_escrow=1.0)
    )
    assert vmc.clear_fault("PAY-104", by="admin") is True
    await asyncio.sleep(0.05)
    assert store.load() is None
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "pass"
    vmc.cancel_pending_tasks()


async def test_clear_pay_104_fails_closed_when_evidence_file_persists(tmp_path):
    vmc, avail, store = _boot_with(
        tmp_path, SessionSnapshot(state="interacting_with_user", credit_escrow=1.0)
    )
    store.clear = lambda: False
    assert vmc.clear_fault("PAY-104", by="admin") is False
    assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is True
    rows = {r["name"]: r for r in avail.table()}
    assert rows["transaction_certain"]["state"] == "fail"
    vmc.cancel_pending_tasks()


async def test_session_file_written_during_sale_and_cleared_after(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    await vmc._handle_mqtt_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )

    vmc.deposit_funds(2.5, payment_method="cash_bill")
    await asyncio.sleep(0.05)
    snap = store.load()
    assert snap is not None and snap.credit_escrow == 2.5

    vmc.select_product(0)
    await asyncio.sleep(1.2)  # _process_payment runs after 1s
    assert vmc.state == "dispensing"
    await asyncio.sleep(0.05)
    snap = store.load()
    assert snap.state == "dispensing" and snap.dispense_slot == 0

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": 0, "state": "complete"}
    )
    await asyncio.sleep(0.05)
    assert vmc.state == "idle"
    assert store.load() is None
    vmc.cancel_pending_tasks()


def test_reconcile_session_is_a_documented_stub():
    vmc = make_vmc()
    assert vmc.reconcile_session() is None


# --- Finding A: FSM state published after the transition, not before ---


async def test_error_occurred_and_reset_publish_destination_state_to_availability():
    """error_occurred() must flip fsm_ok immediately, and reset_state() must
    restore it — both require the destination state, not the source state, to
    be published to Availability."""
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)
    avail.set_payment_device("coin_acceptor", "ready")
    assert avail.sale_available("water")[0] is True

    vmc.error_occurred()
    assert avail.sale_available("water")[0] is False
    assert "fsm_ok" in avail.sale_available("water")[1]
    assert avail.payment_enabled is True

    vmc.reset_state()
    assert avail.sale_available("water")[0] is True
    vmc.cancel_pending_tasks()


async def test_status_publish_carries_destination_state():
    """The last 'status' MQTT publish after a transition must show the
    transition's destination state, not the state it started from."""
    vmc, monitor, avail, published = _wired_vmc()
    _all_alive(monitor, vmc)

    vmc.start_interaction()
    await asyncio.sleep(0)
    statuses = [p for t, p in published if t == "status"]
    assert statuses[-1].state == "interacting_with_user"

    vmc.error_occurred()
    await asyncio.sleep(0)
    statuses = [p for t, p in published if t == "status"]
    assert statuses[-1].state == "error"
    vmc.cancel_pending_tasks()


# --- Finding B: shutdown drains in-flight persistence writes ---


async def test_drain_persistence_awaits_pending_session_write(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)

    vmc.deposit_funds(1.0)
    await vmc.drain_persistence()

    snap = store.load()
    assert snap is not None
    assert snap.credit_escrow == 1.0
    vmc.cancel_pending_tasks()


async def test_cancel_pending_tasks_never_cancels_persistence(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    vmc, monitor, avail, published = _wired_vmc()
    vmc.set_session_store(store)

    vmc.deposit_funds(1.0)
    vmc.cancel_pending_tasks()
    await vmc.drain_persistence()

    assert vmc._persist_tasks
    assert all(not t.cancelled() for t in vmc._persist_tasks)
    snap = store.load()
    assert snap is not None
    assert snap.credit_escrow == 1.0


# --- Finding: dispensing snapshot persisted before cmd/dispense is sent ---


async def test_dispense_snapshot_persisted_before_dispense_command(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_session_store(store)

    published: list = []

    class FakeMQTT:
        def register(self, *a, **k):
            pass

        async def publish(self, topic, payload, qos=1, retain=False):
            if topic == "cmd/dispense":
                snap = store.load()
                assert snap is not None
                assert snap.state == "dispensing"
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMQTT())
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 2.50

    vmc._process_payment()

    # Bounded wait instead of a fixed sleep: this test failed once in CI and
    # passed on an unchanged re-run, because 50ms is enough time for
    # _process_payment's background task to publish cmd/dispense on a
    # developer machine but not reliably enough on a loaded CI runner. Poll
    # for the publish instead of gambling on a fixed delay; the assertions
    # inside FakeMQTT.publish (snapshot persisted with state "dispensing"
    # *before* the publish) still run for real on whichever iteration the
    # publish actually lands, so this stays a wait for the real event, not a
    # race that can pass without ever running them.
    deadline = asyncio.get_running_loop().time() + 2.0
    while not any(t == "cmd/dispense" for t, _ in published):
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(
                "cmd/dispense was never published within 2s of "
                f"_process_payment(); published so far: {published!r}"
            )
        await asyncio.sleep(0.01)

    assert any(t == "cmd/dispense" for t, _ in published)
    vmc.cancel_pending_tasks()


# --- Finding: retained status republished on MQTT (re)connect ---


async def test_status_republished_on_mqtt_connect():
    vmc, monitor, avail, published = _wired_vmc()
    vmc.on_mqtt_connection(True)
    await asyncio.sleep(0)

    statuses = [p for t, p in published if t == "status"]
    assert statuses
    assert statuses[-1].state == "idle"
    vmc.cancel_pending_tasks()
