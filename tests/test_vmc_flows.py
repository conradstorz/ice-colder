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
from services.event_recorder import EventRecorder, SaleRecordingFailed
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


async def test_record_sale_totally_lost_preserves_pay104_recovery_not_data_101(
    tmp_path,
):
    """Third rung of the durability ladder: the sales insert can fail AND
    the journal fallback can also fail (a full or read-only data volume) --
    services.event_recorder.SaleRecordingFailed signals exactly that. The
    vend must still complete (a storage problem must never fail the vend),
    but the sale must not simply vanish: this must raise PAY-104 (not the
    ordinary DATA-101) and must NOT clear pending_sale_shares, so the
    'dispensing' snapshot _process_payment already wrote to disk survives
    untouched (_persist_session refuses to touch the file once PAY-104 is
    active) as the sale's only remaining record -- recoverable through the
    existing Health > Faults record/discard flow with no reboot required.
    """
    store_path = tmp_path / "session.json"
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    vmc.set_session_store(SessionStore(store_path))
    vmc.set_availability(Availability())

    class TotallyFailingRecorder:
        def __init__(self):
            self.record_sale_calls = 0

        def record(self, event_type, value=1.0, metadata=None):
            pass

        def record_sale(self, sku, name, slot, price, methods, ts=None):
            self.record_sale_calls += 1
            raise SaleRecordingFailed("db insert and journal fallback both failed")

    recorder = TotallyFailingRecorder()
    vmc.set_event_recorder(recorder)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(2.50, payment_method="cash_bill")

    vmc._process_payment()
    assert vmc.state == "dispensing"
    await vmc.drain_persistence()  # the 'dispensing' snapshot is now on disk

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {"slot": vmc.products[0].slot, "state": "complete"},
    )

    assert recorder.record_sale_calls == 1  # the failure branch was entered
    assert vmc.state == "idle"  # the vend still completed regardless
    assert vmc.selected_product is None

    faults = {f["code"] for f in vmc.active_faults()}
    assert "PAY-104" in faults
    assert "DATA-101" not in faults  # not treated as the ordinary path

    # The sale must be recoverable through the existing PAY-104 flow, in
    # this same process, with no reboot -- proving the on-disk snapshot
    # genuinely survived, not merely that some in-memory flag is set.
    pending = vmc.pending_sale_for_recovery()
    assert pending is not None, "sale evidence was lost -- nothing to recover"
    assert pending["sku"] == "ICE-1"
    assert pending["price"] == pytest.approx(2.50)
    assert pending["methods"] == {"cash_bill": 2.50}

    # And the on-disk file itself, read on a separate SessionStore/second
    # connection to the same path -- not vmc's own in-memory state.
    reloaded = SessionStore(store_path).load()
    assert reloaded is not None
    assert reloaded.pending_sale_shares == {"cash_bill": 2.50}
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


# --- Task 10: the maintenance lease (system-tests design §2.2) ---


class TestMaintenanceLease:
    async def test_granted_when_idle_with_zero_escrow(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())

        granted, reason = vmc.begin_maintenance("user-1", "sess-1")

        assert granted is True
        assert reason is None
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.holder_user_id == "user-1"
        assert vmc.maintenance_hold.holder_session_id == "sess-1"
        assert vmc.maintenance_hold.runs_in_flight == 0
        assert vmc.maintenance_hold.release_requested is False

    async def test_refused_when_fsm_not_idle(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.machine.set_state("interacting_with_user")

        granted, reason = vmc.begin_maintenance("user-1", "sess-1")

        assert granted is False
        assert reason == "machine is mid-sale"
        assert vmc.maintenance_hold is None

    async def test_refused_when_escrow_nonzero_even_while_idle(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        assert vmc.state == "idle"
        vmc.credit_escrow = 1.00

        granted, reason = vmc.begin_maintenance("user-1", "sess-1")

        assert granted is False
        assert "credit" in reason
        assert vmc.maintenance_hold is None

    async def test_refused_when_lease_exists_names_holder(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        granted1, _ = vmc.begin_maintenance("owner-1", "sess-a")
        assert granted1 is True

        granted2, reason2 = vmc.begin_maintenance("tech-2", "sess-b")

        assert granted2 is False
        assert "owner-1" in reason2
        assert vmc.maintenance_hold.holder_user_id == "owner-1"

    async def test_svc_102_raised_on_grant_and_cleared_on_release(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())

        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True
        assert "SVC-102" in [f["code"] for f in vmc.active_faults()]

        released = vmc.end_maintenance("sess-1")
        assert released is True
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        assert vmc.maintenance_hold is None

    async def test_payment_disabled_while_held_and_restored_after_release(self):
        vmc, monitor, avail, published = _wired_vmc()
        await asyncio.sleep(0)  # let any initial publish settle

        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True
        await asyncio.sleep(0)  # let the fire-and-forget publish task run

        assert avail.payment_enabled is False
        enable_cmds = [p for t, p in published if t == "cmd/payment/enable"]
        assert enable_cmds, "expected cmd/payment/enable to have been published"
        assert enable_cmds[-1].accept is False

        released = vmc.end_maintenance("sess-1")
        assert released is True
        await asyncio.sleep(0)

        assert avail.payment_enabled is True
        enable_cmds_after = [p for t, p in published if t == "cmd/payment/enable"]
        assert enable_cmds_after[-1].accept is True

    async def test_credit_during_lease_is_refunded_and_escrow_stays_zero(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        client = RecordingClient()
        vmc.set_mqtt_client(client)
        rec = FakeEventRecorder()
        vmc.set_event_recorder(rec)
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        vmc.deposit_funds(1.00, payment_method="cash_coin")
        await asyncio.sleep(0)

        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []
        refund_cmds = client.refund_commands()
        assert len(refund_cmds) == 1
        assert refund_cmds[0].amount == 1.00
        assert refund_cmds[0].reason == "maintenance"

        rid = refund_cmds[0].request_id
        await vmc._handle_mqtt_refund_ack(
            "cmd/payment/refund/ack",
            {"request_id": rid, "status": "ok", "amount_returned": 1.00},
        )

        assert (
            "refund",
            1.00,
            {"request_id": rid, "reason": "maintenance"},
        ) in rec.events
        # The money must never become spendable escrow after the hold ends.
        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []

    async def test_end_maintenance_by_non_holder_is_refused(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True

        result = vmc.end_maintenance("sess-b")

        assert result is False
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.holder_session_id == "sess-a"

    async def test_release_with_run_in_flight_defers_then_settles(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True
        vmc._maintenance_run_started()
        assert vmc.maintenance_hold.runs_in_flight == 1

        result = vmc.end_maintenance("sess-a")

        assert result is True
        assert vmc.maintenance_hold is not None  # not released yet
        assert vmc.maintenance_hold.release_requested is True

        vmc._maintenance_run_finished()

        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]

    async def test_idle_timer_never_releases_with_run_in_flight(self):
        # Deterministic like the take-over tests below: backdate
        # last_activity_at past the deadline and invoke the real timer
        # callback directly, instead of overriding
        # MAINTENANCE_IDLE_TIMEOUT_SECONDS and waiting on a real
        # asyncio.sleep for the scheduled task to fire.
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True
        vmc._maintenance_run_started()
        vmc.maintenance_hold.last_activity_at -= (
            vmc.MAINTENANCE_IDLE_TIMEOUT_SECONDS + 1
        )

        vmc._maintenance_idle_expired()

        # The timer fired, but a run is in flight: it must defer, not release.
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.release_requested is True
        assert "SVC-102" in [f["code"] for f in vmc.active_faults()]

        vmc._maintenance_run_finished()

        assert vmc.maintenance_hold is None

    async def test_idle_timer_releases_lease_when_no_runs_in_flight(self):
        # Same deterministic mechanism, but the positive case: idle past the
        # deadline with zero runs in flight must actually release the lease
        # -- not just flip release_requested. Reaches VMC._maintenance_idle_expired's
        # zero-runs branch -> _release_maintenance_hold -> clear_fault ->
        # real Availability._recompute -> VMC.publish_payment_enable -> the
        # fake MQTT client's publish, via _wired_vmc()'s real Availability.
        vmc, monitor, avail, published = _wired_vmc()
        await asyncio.sleep(0)  # let any initial publish settle

        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True
        await asyncio.sleep(0)
        assert avail.payment_enabled is False
        assert "SVC-102" in [f["code"] for f in vmc.active_faults()]

        vmc.maintenance_hold.last_activity_at -= (
            vmc.MAINTENANCE_IDLE_TIMEOUT_SECONDS + 1
        )

        vmc._maintenance_idle_expired()
        await asyncio.sleep(0)  # let the fire-and-forget publish task run

        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        assert avail.payment_enabled is True
        enable_cmds = [p for t, p in published if t == "cmd/payment/enable"]
        assert enable_cmds, "expected cmd/payment/enable to have been published"
        assert enable_cmds[-1].accept is True

    async def test_cancelled_run_still_decrements_runs_in_flight(self):
        # Reaches maintenance_test_run's `finally` via a real
        # asyncio.CancelledError propagating out of the `with` body --
        # Python's context-manager protocol runs `finally` on any exception,
        # CancelledError (a BaseException) included, but nothing proved that
        # until now.
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True

        entered = asyncio.Event()

        async def run():
            with vmc.maintenance_test_run():
                assert vmc.maintenance_hold.runs_in_flight == 1
                entered.set()
                await asyncio.sleep(3600)  # never elapses; cancelled below

        task = asyncio.get_running_loop().create_task(run())
        await entered.wait()
        assert vmc.maintenance_hold.runs_in_flight == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert vmc.maintenance_hold.runs_in_flight == 0

    async def test_concurrent_runs_release_once_on_last_settle(self):
        # Two genuinely overlapping runs (two tasks both suspended inside
        # maintenance_test_run, woken via a shared Event) prove the
        # 0->1->2->1->0 accounting and that release-on-last-settle fires
        # exactly once -- not when the first of two in-flight runs settles.
        # The mutation below (dropping the runs_in_flight == 0 guard in
        # _maintenance_run_finished) makes the first settle release early;
        # this test's mid-point assertion (still held, still disabled) is
        # what catches that. It also proves _release_maintenance_hold's
        # idempotence (via clear_fault's own "already cleared" guard) is
        # what makes a stray extra release call harmless.
        vmc, monitor, avail, published = _wired_vmc()
        await asyncio.sleep(0)
        granted, _ = vmc.begin_maintenance("user-1", "sess-a")
        assert granted is True
        await asyncio.sleep(0)
        assert avail.payment_enabled is False

        # Two separate gates so the two runs can be settled one at a time,
        # deterministically -- releasing a single shared Event wakes both
        # waiters' continuations in the same event-loop pass, which would
        # let t2 race ahead to completion before the test observes the
        # mid-point (runs_in_flight == 1, still held) at all.
        gate1 = asyncio.Event()
        gate2 = asyncio.Event()

        async def run(gate):
            with vmc.maintenance_test_run():
                await gate.wait()

        t1 = asyncio.get_running_loop().create_task(run(gate1))
        await asyncio.sleep(0)
        assert vmc.maintenance_hold.runs_in_flight == 1

        t2 = asyncio.get_running_loop().create_task(run(gate2))
        await asyncio.sleep(0)
        assert vmc.maintenance_hold.runs_in_flight == 2

        # Request release while both runs are in flight: must defer.
        result = vmc.end_maintenance("sess-a")
        assert result is True
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.release_requested is True

        gate1.set()
        await t1
        # Only one of the two runs has settled: the lease must still be
        # held and payment must still be disabled.
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.runs_in_flight == 1
        assert "SVC-102" in [f["code"] for f in vmc.active_faults()]
        assert avail.payment_enabled is False

        gate2.set()
        await t2
        await asyncio.sleep(0)  # let the release's fire-and-forget publish run

        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        assert avail.payment_enabled is True
        enable_true = [
            p for t, p in published if t == "cmd/payment/enable" and p.accept is True
        ]
        assert len(enable_true) == 1, (
            "expected exactly one re-enable, not a double release"
        )

        # A stray extra release call must be a no-op: clear_fault's own
        # "already cleared" guard stops it from re-pushing availability, so
        # no second enable is published.
        vmc._release_maintenance_hold(by="stray")
        await asyncio.sleep(0)
        enable_true_after = [
            p for t, p in published if t == "cmd/payment/enable" and p.accept is True
        ]
        assert len(enable_true_after) == 1

    async def test_takeover_refused_with_run_in_flight(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.begin_maintenance("user-1", "sess-a")
        vmc._maintenance_run_started()

        granted, reason = vmc.take_over_maintenance("user-2", "sess-b")

        assert granted is False
        assert vmc.maintenance_hold.holder_user_id == "user-1"
        vmc._maintenance_run_finished()

    async def test_takeover_refused_before_60s_idle(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.begin_maintenance("user-1", "sess-a")

        granted, reason = vmc.take_over_maintenance("user-2", "sess-b")

        assert granted is False
        assert "idle" in reason
        assert vmc.maintenance_hold.holder_user_id == "user-1"

    async def test_takeover_permitted_after_60s_idle_records_who(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.begin_maintenance("user-1", "sess-a")
        vmc.maintenance_hold.last_activity_at -= (
            vmc.MAINTENANCE_TAKEOVER_IDLE_SECONDS + 1
        )

        granted, reason = vmc.take_over_maintenance("user-2", "sess-b")

        assert granted is True
        assert reason is None
        assert vmc.maintenance_hold.holder_user_id == "user-2"
        assert vmc.maintenance_hold.holder_session_id == "sess-b"

    async def test_failed_run_still_decrements_runs_in_flight(self):
        vmc = make_vmc2()
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.begin_maintenance("user-1", "sess-a")

        with pytest.raises(ValueError):
            with vmc.maintenance_test_run():
                assert vmc.maintenance_hold.runs_in_flight == 1
                raise ValueError("simulated run failure")

        assert vmc.maintenance_hold.runs_in_flight == 0


def _test_run_vmc(products=None):
    """A wired-up VMC plus a FakeEventRecorder and RecordingClient, for
    VMC.run_test_sale tests. Mirrors make_vmc2()'s default two-product
    catalog (ICE-1 $2.50 slot 0, WATER-1 $1.00 slot 1) unless overridden."""
    cfg = ConfigModel()
    cfg.physical.products = products or [
        Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0),
        Product(sku="WATER-1", name="Water", price=1.00, slot=1),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    vmc.set_mqtt_client(client)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    return vmc, rec, client


class TestRunTestSale:
    """VMC.run_test_sale and the per-sale is_test flag (system-tests design
    §2.3) -- the task most likely to corrupt part 3's sales ledger.

    Every test drives run_test_sale as a background task and calls
    vmc._process_payment() directly (rather than waiting a real 1s for
    select_product's own scheduled call) and, where a dispense timeout is
    needed, vmc._dispense_timed_out() directly (rather than waiting a real
    dispense_timeout_seconds) -- the same "call the production callback
    directly" pattern already used throughout this file and in
    TestMaintenanceLease's idle-timer tests, so nothing here depends on
    real wall-clock timing.
    """

    async def test_run_test_sale_without_lease_is_refused(self):
        """Reaches run_test_sale -> maintenance_test_run() ->
        _maintenance_run_started's `if hold is None: raise RuntimeError`
        (Task 10) -- run_test_sale's own refusal path, before it ever
        touches escrow or the catalog."""
        vmc, rec, client = _test_run_vmc()

        with pytest.raises(RuntimeError):
            await vmc.run_test_sale("ICE-1")

        assert vmc._sale_is_test is False
        assert rec.sales == []
        assert client.published == []

    async def test_deposit_funds_test_method_accepted_other_methods_refunded(self):
        """Reaches deposit_funds's lease branch (Task 10 + this task's
        `and payment_method != "test"` exception)."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        # A stray non-"test" credit during the lease is still refunded,
        # not escrowed -- Task 10's existing lease branch, unchanged.
        vmc.deposit_funds(1.00, payment_method="cash_coin")
        await asyncio.sleep(0)
        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []
        assert len(client.refund_commands()) == 1
        assert client.refund_commands()[0].reason == "maintenance"

        # run_test_sale's own "test" credit is the one exception: it must
        # fall through to the normal escrow path instead of being refunded.
        vmc.deposit_funds(1.00, payment_method="test")
        await asyncio.sleep(0)
        assert vmc.credit_escrow == 1.00
        assert [c.method for c in vmc.escrow_credits] == ["test"]
        assert len(client.refund_commands()) == 1  # no new refund

    async def test_dispensed_test_sale_records_test_run_not_sale_or_dispense(self):
        """Reaches _handle_mqtt_dispenser's DispenserOutcome.complete
        branch, its `if self._sale_is_test:` arm -- the real completion
        handler, not a stub."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        assert vmc.state == "interacting_with_user"
        assert vmc._sale_is_test is True

        vmc._process_payment()
        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"test": 2.50}

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)

        assert result.sku == "ICE-1"
        assert result.outcome == "dispensed"
        assert result.fault_code is None
        assert "dispensing" in result.path
        assert result.path[-1] == "idle"

        assert rec.sales == []
        assert not any(t == "dispense" for t, *_ in rec.events)
        test_run_events = [e for e in rec.events if e[0] == "test_run"]
        assert len(test_run_events) == 1
        assert test_run_events[0][2]["sku"] == "ICE-1"
        assert test_run_events[0][2]["outcome"] == "dispensed"

        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []
        assert client.refund_commands() == []
        assert vmc._sale_is_test is False
        assert vmc.pending_sale_shares is None

    async def test_dispensed_test_sale_survives_lease_released_mid_run(self):
        """The decisive test (system-tests design §2.3): Task 10's rule
        that a lease cannot be released while a run is in flight is the
        belt; this bypasses it directly (forcing maintenance_hold to None
        out from under the in-flight run, which begin/end_maintenance
        themselves would refuse to do) to prove the braces -- is_test
        lives on the sale, not the lease, so the completion handler must
        still treat this as a test sale with no lease at all."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        assert vmc.state == "dispensing"

        vmc._maintenance_hold = None  # simulate the lease vanishing mid-run

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)

        assert result.outcome == "dispensed"
        assert rec.sales == []
        assert not any(t == "dispense" for t, *_ in rec.events)
        assert any(t == "test_run" for t, *_ in rec.events)

    async def test_vend_failed_test_sale_returns_code_and_no_refund_published(self):
        """A single-product catalog so the failing product's lockout
        empties _sellable_products(), reaching _fail_vend's "no sellable
        products remain" branch -- the one that would otherwise call
        request_refund and publish a real cmd/payment/refund."""
        vmc, rec, client = _test_run_vmc(
            products=[Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0)]
        )
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        assert vmc.state == "dispensing"

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "jam"}
        )
        result = await asyncio.wait_for(task, timeout=5)

        assert result.outcome == "vend_failed"
        assert result.fault_code == "ICE-401"
        assert vmc._lockouts == {
            "ICE-1": FaultCode.ICE_401
        }  # a real fault, real lockout

        assert rec.sales == []
        assert client.refund_commands() == []  # the key assertion
        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []
        assert any(t == "test_run" for t, *_ in rec.events)

    async def test_timeout_test_sale_returns_timeout_distinct_from_vend_failed(self):
        """Reaches _dispense_timed_out (the real timeout callback the
        scheduled dispense-timeout task invokes) directly, distinct from
        the DispenserOutcome-driven vend_failed path above."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        assert vmc.state == "dispensing"
        assert vmc._dispense_timeout_task is not None

        vmc._dispense_timed_out()
        result = await asyncio.wait_for(task, timeout=5)

        assert result.outcome == "timeout"
        assert result.fault_code is None
        assert vmc._lockouts == {}  # PAY-102 (vend_failed severity) never locks

        assert rec.sales == []
        assert client.refund_commands() == []
        assert vmc.credit_escrow == 0.0
        assert vmc.escrow_credits == []
        test_run_events = [e for e in rec.events if e[0] == "test_run"]
        assert len(test_run_events) == 1
        assert test_run_events[0][2]["outcome"] == "timeout"

    async def test_production_sale_immediately_after_test_sale_records_normally(self):
        """Part 3's guarantee still holds right after a test sale on the
        same VMC instance: a production sale records its row with its FIFO
        method shares, catching a regression here rather than in part 3's
        own tests."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        test_result = await asyncio.wait_for(task, timeout=5)
        assert test_result.outcome == "dispensed"
        assert rec.sales == []  # the test sale itself recorded nothing

        released = vmc.end_maintenance("sess-1")
        assert released is True
        assert vmc.maintenance_hold is None

        vmc.deposit_funds(1.00, payment_method="cash_coin")
        vmc.deposit_funds(1.50, payment_method="card")
        vmc.select_product(1)  # WATER-1, price 1.00
        vmc._process_payment()
        assert vmc.state == "dispensing"
        assert vmc.pending_sale_shares == {"cash_coin": 1.00}

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 1, "state": "complete"}
        )

        assert len(rec.sales) == 1
        sku, name, slot, price, methods = rec.sales[0]
        assert sku == "WATER-1"
        assert price == 1.00
        assert methods == {"cash_coin": 1.00}
        assert vmc.credit_escrow == 1.50
        assert [c.method for c in vmc.escrow_credits] == ["card"]

    async def test_run_test_sale_publishes_real_cmd_dispense(self):
        """Round-1 fix, minor finding 1: spec §2.3's whole point is that a
        test sale exercises the production command path, but nothing
        previously asserted that `cmd/dispense` was actually published --
        only that the FSM reached `dispensing`. Reaches the same
        `dispense_product` -> `cmd/dispense` publish a real sale uses,
        checked against the actual recorded MQTT command, not inferred
        from state."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        assert vmc.state == "dispensing"

        # dispense_product's cmd/dispense publish is fire-and-forget (see
        # test_dispense_snapshot_persisted_before_dispense_command's own
        # note on this); drain_persistence() awaits that same tracked task
        # to completion, which only happens after the publish call inside
        # it, so this is a wait for the real event rather than a guess.
        await vmc.drain_persistence()

        dispense_cmds = [p for t, p in client.published if t == "cmd/dispense"]
        assert len(dispense_cmds) == 1
        assert dispense_cmds[0].slot == 0  # ICE-1's slot

        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)
        assert result.outcome == "dispensed"
        # Still exactly one cmd/dispense -- completion must not re-publish.
        assert len([p for t, p in client.published if t == "cmd/dispense"]) == 1

    async def test_crashed_test_sale_offers_no_recovery_and_raises_no_pay104_after_restart(
        self, tmp_path
    ):
        """Round-1 fix for the Critical defect: a test sale that crashes
        mid-dispense must never be offered for PAY-104 recovery and must
        never raise PAY-104 at all -- a test sale risked no real money, so
        nagging the operator about one is a false alarm.

        Drives run_test_sale for real up to the exact point _process_
        payment persists the 'dispensing' snapshot (Task 4's unmodified
        persistence path -- unchanged by this fix), which now carries
        is_test=True because VMC._snapshot() reads self._sale_is_test.
        Then, rather than reading any flag off the live vmc1 object, this
        constructs a FRESH SessionStore and a FRESH VMC from the same
        on-disk file -- a real process boundary, exactly the shape
        test_pay_104_snapshot_exposes_pending_sale_after_crash_mid_dispense
        uses for the production case -- and asserts recovery finds nothing
        and no PAY-104 fires.

        Production paths reached: VMC._process_payment (unmodified),
        VMC._snapshot (this fix's `is_test=self._sale_is_test`),
        VMC.set_session_store (this fix's is_test boot branch), and
        VMC.pending_sale_for_recovery (this fix's is_test guard).
        """
        store_path = tmp_path / "session.json"
        vmc, rec, client = _test_run_vmc()
        vmc.set_session_store(SessionStore(store_path))
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()  # persists the 'dispensing' snapshot
        assert vmc.state == "dispensing"
        await vmc.drain_persistence()  # the save is fire-and-forget -- wait for it

        # Positive control: the snapshot really is on disk, open, and
        # test-flagged -- before asserting anything about recovery from it.
        raw = SessionStore(store_path).load()
        assert raw is not None
        assert raw.error is None
        assert raw.is_test is True
        assert raw.pending_sale_shares == {"test": 2.50}
        assert raw.is_open() is True

        # Simulate the crash: nothing else on vmc1 (including run_test_sale's
        # own `finally`) ever runs.
        vmc.cancel_pending_tasks()

        # A fresh process boundary: a brand-new SessionStore and VMC loading
        # the same file, exactly as a real restart-after-crash would.
        vmc2, rec2, client2 = _test_run_vmc()
        vmc2.set_session_store(SessionStore(store_path))

        assert "PAY-104" not in {f["code"] for f in vmc2.active_faults()}
        assert vmc2.pending_sale_for_recovery() is None
        vmc2.cancel_pending_tasks()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_pending_sale_for_recovery_refuses_test_snapshot_even_with_pay104_forced(
        self, tmp_path
    ):
        """Defence-in-depth unit test for pending_sale_for_recovery()'s own
        is_test guard, independent of the boot-time gate exercised by the
        crash test above: forces PAY-104 active and a test-flagged
        snapshot onto disk directly, bypassing set_session_store's own
        gate entirely (attaching the store via `_session_store` directly,
        never through `set_session_store()`), to prove this second
        chokepoint refuses on its own merits -- not only because the boot
        gate already keeps the two states from ever coexisting in
        practice. This is exactly the scenario the report's rationale
        describes: some future call site raises PAY-104 while a stale
        test-sale snapshot happens to still be on disk.

        Production path reached: VMC.pending_sale_for_recovery (this
        fix's is_test guard), independent of VMC.set_session_store.
        """
        store_path = tmp_path / "session.json"
        vmc, rec, client = _test_run_vmc()
        session_store = SessionStore(store_path)
        vmc._session_store = session_store  # bypass set_session_store's own gate
        session_store.save(
            SessionSnapshot(
                state="dispensing",
                credit_escrow=0.0,
                selected_sku="ICE-1",
                dispense_slot=0,
                pending_sale_shares={"test": 2.50},
                is_test=True,
            )
        )
        vmc._raise_fault(FaultCode.PAY_104, outcome="test, forced directly")

        # Positive control: the fault really is active before asserting
        # what the accessor does about it.
        assert "PAY-104" in {f["code"] for f in vmc.active_faults()}
        assert vmc.pending_sale_for_recovery() is None

    async def test_crashed_production_sale_is_still_offered_for_recovery_with_fifo_shares(
        self, tmp_path
    ):
        """Mirror image of the test above (verification rule 4): a fix that
        protects the ledger by breaking PAY-104 for real sales would be
        worse than the defect it fixes. A genuine production sale crashing
        mid-dispense on the same VMC/session-store plumbing used throughout
        this class must still raise PAY-104 and still be offered for
        recovery with its real FIFO method shares, across the same fresh
        SessionStore/VMC process boundary as the test-sale case.

        Production paths reached: VMC.deposit_funds, VMC.select_product,
        VMC._process_payment/_consume_credits_fifo (all unmodified),
        VMC._snapshot (is_test=False for a real sale), VMC.set_session_store
        (the pre-existing is_open() branch, untouched by this fix), and
        VMC.pending_sale_for_recovery (returns the pending sale as before).
        """
        store_path = tmp_path / "session.json"
        vmc, rec, client = _test_run_vmc()
        vmc.set_session_store(SessionStore(store_path))

        vmc.machine.set_state("interacting_with_user")
        product = vmc.products[0]  # ICE-1, price 2.50
        vmc.selected_product = product
        vmc.deposit_funds(2.00, payment_method="cash_bill")
        vmc.deposit_funds(0.50, payment_method="card")

        vmc._process_payment()
        assert vmc.state == "dispensing"
        await vmc.drain_persistence()

        raw = SessionStore(store_path).load()
        assert raw is not None
        assert raw.is_test is False
        assert raw.pending_sale_shares == {"cash_bill": 2.00, "card": 0.50}

        vmc.cancel_pending_tasks()  # simulate the crash

        vmc2, rec2, client2 = _test_run_vmc()
        vmc2.set_session_store(SessionStore(store_path))

        assert "PAY-104" in {f["code"] for f in vmc2.active_faults()}
        pending = vmc2.pending_sale_for_recovery()
        assert pending is not None
        assert pending["sku"] == "ICE-1"
        assert pending["methods"] == {"cash_bill": 2.00, "card": 0.50}
        assert pending["price"] == 2.50
        vmc2.cancel_pending_tasks()


class TestRunTestSaleRunContext:
    """Task 13b: run_test_sale's test_run row now carries run_id/user_id/
    user_name/subsystem/command/params/status/checks/verdict/note (spec
    §4's metadata shape) instead of the bare sku/outcome/fault_code/path
    the pre-13b row had, and TestSaleResult carries the same run_id back
    to the caller -- both are what make a simulated sale's row
    verdictable via POST /tests/runs/{run_id}/verdict at all.
    """

    async def test_result_and_row_share_one_run_id_and_ok_status(self):
        """Reaches run_test_sale's dispensed-outcome path and its metadata
        dict construction directly (FakeEventRecorder, `rec.events`).

        Mutation proof: reverted the metadata dict to the pre-13b shape
        (`{"sku": sku, "outcome": outcome, "fault_code": fault_code,
        "path": path}`, dropping run_id/user_id/user_name/status/etc) and
        also dropped `run_id=run_id` from the returned TestSaleResult.
        Result: this test failed with `AttributeError: 'TestSaleResult'
        object has no attribute 'run_id'` at `result.run_id` -- a real
        AttributeError, not a wrong-value assertion. Restored both edits
        -- passed again, and test_dispensed_test_sale_records_test_run_
        not_sale_or_dispense (unrelated to run_id) kept passing throughout
        (mutation proof for THAT test lives on it already, above).
        """
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(
            vmc.run_test_sale("ICE-1", user_id="user-1", user_name="Ada Owner")
        )
        await asyncio.sleep(0)
        vmc._process_payment()
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)

        assert result.run_id
        test_run_events = [e for e in rec.events if e[0] == "test_run"]
        assert len(test_run_events) == 1
        meta = test_run_events[0][2]
        assert meta["run_id"] == result.run_id
        assert meta["user_id"] == "user-1"
        assert meta["user_name"] == "Ada Owner"
        assert meta["command"] == "simulated_sale"
        assert meta["subsystem"] is None
        assert meta["params"] == {"sku": "ICE-1"}
        assert meta["status"] == "ok"
        assert meta["verdict"] is None
        assert meta["note"] is None

    async def test_status_is_failed_for_a_non_dispensed_outcome(self):
        """outcome != "dispensed" -> status "failed" (run_test_sale's own
        derivation) -- reaches the timeout outcome path, a real one (not a
        mock), via vmc._dispense_timed_out()."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        vmc._dispense_timed_out()
        result = await asyncio.wait_for(task, timeout=5)

        assert result.outcome == "timeout"
        test_run_events = [e for e in rec.events if e[0] == "test_run"]
        assert len(test_run_events) == 1
        assert test_run_events[0][2]["status"] == "failed"
        assert test_run_events[0][2]["run_id"] == result.run_id

    async def test_user_id_and_name_default_to_none_for_a_bare_call(self):
        """Backward compatibility: every pre-13b caller (this file's own
        TestRunTestSale class above, which never passes user_id/user_name)
        must keep working unchanged -- reaches run_test_sale's keyword-only
        defaults directly."""
        vmc, rec, client = _test_run_vmc()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        vmc._process_payment()
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)

        test_run_events = [e for e in rec.events if e[0] == "test_run"]
        assert test_run_events[0][2]["user_id"] is None
        assert test_run_events[0][2]["user_name"] is None
        assert result.run_id  # still minted even with no caller identity

    async def test_run_id_is_genuinely_verdictable_via_real_event_recorder(
        self, tmp_path
    ):
        """End-to-end through a REAL EventRecorder (not FakeEventRecorder):
        proves a simulated sale's row can actually be located and updated
        by EventRecorder.update_metadata(run_id, ...) -- the same
        mechanism POST /tests/runs/{run_id}/verdict uses -- and that
        exactly one test_run row exists for the one call made.

        Mutation proof: changed run_test_sale's `run_id = uuid4().hex` to
        `run_id = "not-unique"` (a constant). This test still passed on
        its own (a single call has no collision to expose), but is
        exactly the row-uniqueness hazard EventRecorder._update_metadata's
        own docstring calls out ("more than one row matching run_id...
        updating all of them") -- restored `uuid4().hex` since a constant
        run_id would silently merge verdicts across unrelated runs the
        moment two simulated sales ever happened. The genuine failure
        this test DOES catch: dropping `run_id=run_id` from the returned
        TestSaleResult (see the class's first test's mutation proof,
        which reaches that same line) breaks `result.run_id` here too
        with the same AttributeError.
        """
        cfg = ConfigModel()
        cfg.physical.products = [
            Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0)
        ]
        vmc = VMC(config=cfg)
        vmc.attach_to_loop(asyncio.get_running_loop())
        vmc.set_mqtt_client(RecordingClient())
        recorder = EventRecorder(db_path=str(tmp_path / "events.db"))
        vmc.set_event_recorder(recorder)

        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True

        task = asyncio.get_running_loop().create_task(
            vmc.run_test_sale("ICE-1", user_id="user-1", user_name="Ada")
        )
        await asyncio.sleep(0)
        vmc._process_payment()
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task, timeout=5)
        recorder.flush()

        conn = sqlite3.connect(str(tmp_path / "events.db"))
        try:
            rows = conn.execute(
                "SELECT metadata FROM events WHERE event_type='test_run'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1  # exactly one row for this one simulated sale
        meta = json.loads(rows[0][0])
        assert meta["run_id"] == result.run_id
        assert meta["verdict"] is None

        recorder.update_metadata(result.run_id, verdict="pass", note="tasted fine")
        recorder.flush()

        conn = sqlite3.connect(str(tmp_path / "events.db"))
        try:
            rows = conn.execute(
                "SELECT metadata FROM events WHERE event_type='test_run'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1  # verdict updated IN PLACE, not a second row
        meta = json.loads(rows[0][0])
        assert meta["verdict"] == "pass"
        assert meta["note"] == "tasted fine"


def _test_run_vmc_with_availability(products=None):
    """Like `_test_run_vmc`, but with a REAL `services.availability.
    Availability` wired in -- not a stub -- so `avail.payment_enabled` and
    the `SVC-102` clear-on-release path are genuine production code, not
    stand-ins (whole-branch-fix verification rule 4: "Use a VMC with a
    real Availability attached, or the payment assertions are
    unreachable").

    Adjacent-bug workaround, reported but NOT fixed here (out of scope --
    see `.superpowers/sdd/whole-branch-fix-report.md`): a real
    `Availability`'s `no_critical_fault` row is a Gate.safety row, and
    `Availability._rows_for` only excludes Gate.alert rows from
    `sale_available`/`product_sellable` -- so it is scope-blind and folds
    the maintenance lease's OWN `SVC-102` fault into `product_sellable`
    exactly like a hardware fault, not just into `payment_enabled`. That
    means `run_test_sale`'s own `select_product` call can never succeed
    while ANY lease is held, in a production VMC with a real Availability
    attached (`main.py` always attaches one) -- a genuine, separate defect
    unrelated to the concurrency bug these tests target (confirmed against
    this checkout: `begin_maintenance` followed by `avail.product_sellable
    (product)` returns `(False, [..., "no_critical_fault"])` even with
    every other row passing). `product_sellable` is overridden on this ONE
    instance (not monkeypatched at the class level) purely so these tests
    can reach `select_product`'s success path and thus the concurrency
    code path at all; `payment_enabled`/`payment_blocking_reasons` --
    driven only by the safety rows via `_recompute`, never by this
    override -- are left completely real, which is what makes the
    `SVC-102`/`payment_enabled` assertions below trustworthy.
    """
    cfg = ConfigModel()
    cfg.physical.products = products or [
        Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0),
        Product(sku="WATER-1", name="Water", price=1.00, slot=1),
    ]
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    vmc.set_mqtt_client(client)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    avail = Availability()
    vmc.set_availability(avail)
    avail.product_sellable = lambda product: (True, [])
    return vmc, rec, client, avail


class TestConcurrentTestSaleGuard:
    """Whole-branch-review Critical defect: a second, overlapping
    `run_test_sale` call from the same session -- reachable via
    `web_interface/routes/tests_level.py`'s `_acquire_lease_or_refusal`,
    which deliberately proceeds WITHOUT reacquiring the lease for a
    session that already holds it ("a second command run in the same
    maintenance visit") -- used to silently overwrite the single instance
    attributes `self._test_sale_waiter` and `self._test_sale_path`
    (`controller/vmc.py`). That left the FIRST call's `await waiter`
    suspended forever on a Future nothing would ever resolve again, while
    its `with self.maintenance_test_run():` was still on the stack --
    `runs_in_flight` never decremented, pinning the maintenance lease out
    of service until process restart.

    Every test below drives two `run_test_sale` calls so they GENUINELY
    overlap -- both reach their own `await waiter` suspension (proved via
    `task.done() is False`, not merely `task.done()` never checked) before
    either is settled -- exactly the reachable production shape: neither
    call has been processed into `dispensing` yet, so `select_product`
    (called synchronously, no `await` in between) succeeds for BOTH calls,
    the second silently overwriting `self.selected_product` too, on
    unfixed code. `_test_run_vmc_with_availability` wires a real
    `Availability` so `avail.payment_enabled` and the `SVC-102` clear on
    lease release are real production paths, not stand-ins.
    """

    async def test_second_overlapping_call_is_refused_first_completes_normally(self):
        """THE decisive test for the Critical defect. Reaches:
        `VMC.run_test_sale`'s `_test_sale_in_progress` guard (the new
        refusal, for call #2) and, end to end for call #1, the ordinary
        success path -- `maintenance_test_run`, `select_product`, the real
        `cmd/dispense`-driven `_handle_mqtt_dispenser` completion handler,
        `end_maintenance`, `_release_maintenance_hold`, `clear_fault`, and
        `Availability._recompute` (a real `Availability`, not a stub).

        Call #2 asks for a DIFFERENT sku (WATER-1) than call #1 (ICE-1),
        so this also proves the guard is not accidentally scoped to "same
        sku only".

        Fail-then-pass evidence (captured against 69d7549's behaviour,
        before this fix, and reported verbatim in
        .superpowers/sdd/whole-branch-fix-report.md): call #2 is awaited
        directly (not as a background task) inside `asyncio.wait_for(...,
        timeout=5)` specifically so that if it reaches its own `await
        waiter` and hangs -- which is exactly what happened pre-fix, once
        call #2's `select_product` succeeded (state was still
        `interacting_with_user`, not yet `dispensing`) and silently
        overwrote `self._test_sale_waiter`/`self._test_sale_path` out from
        under call #1 -- the failure surfaces as a bounded
        `TimeoutError`/`asyncio.TimeoutError` after 5s, not a stuck test
        process. `pytest.raises(RuntimeError, match="already in
        progress")` additionally failed to match on that pre-fix
        `TimeoutError` at all (it isn't even a `RuntimeError`), so the
        pre-fix run failed loudly on two independent counts.
        """
        vmc, rec, client, avail = _test_run_vmc_with_availability()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True
        await asyncio.sleep(0)
        assert avail.payment_enabled is False
        assert "SVC-102" in [f["code"] for f in vmc.active_faults()]

        task1 = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        assert vmc.state == "interacting_with_user"
        assert vmc.selected_product is not None
        assert vmc.selected_product.sku == "ICE-1"
        # Genuinely suspended inside `await waiter` -- not merely created,
        # and not yet processed into `dispensing`.
        assert task1.done() is False
        assert vmc.maintenance_hold is not None
        assert vmc.maintenance_hold.runs_in_flight == 1

        with pytest.raises(RuntimeError, match="already in progress"):
            await asyncio.wait_for(vmc.run_test_sale("WATER-1"), timeout=5)

        # The refusal must not have perturbed call #1's in-flight state:
        # still ICE-1 selected, still suspended, still exactly one run
        # counted.
        assert task1.done() is False
        assert vmc.selected_product.sku == "ICE-1"
        assert vmc.maintenance_hold.runs_in_flight == 1

        vmc._process_payment()
        assert vmc.state == "dispensing"
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 0, "state": "complete"}
        )
        result = await asyncio.wait_for(task1, timeout=5)
        assert result.sku == "ICE-1"
        assert result.outcome == "dispensed"

        assert vmc.maintenance_hold.runs_in_flight == 0
        assert vmc._test_sale_in_progress is False

        released = vmc.end_maintenance("sess-1")
        assert released is True
        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        await asyncio.sleep(0)  # let the release's fire-and-forget publish run
        assert avail.payment_enabled is True

    async def test_second_overlapping_call_refused_then_first_cancelled(self):
        """Rule 3's "one being cancelled": call #1 is genuinely suspended
        in `await waiter` (proved the same way as above), call #2 overlaps
        and is refused, and THEN call #1's own task is cancelled
        (simulating a request timeout, a dropped connection, or app
        shutdown while a simulated sale is mid-flight) -- reaches
        `maintenance_test_run`'s `finally` and `run_test_sale`'s own new
        outer `finally` via a real `asyncio.CancelledError` (a
        `BaseException`) propagating out of `await waiter`, not a mock.
        """
        vmc, rec, client, avail = _test_run_vmc_with_availability()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True
        await asyncio.sleep(0)

        task1 = asyncio.get_running_loop().create_task(vmc.run_test_sale("ICE-1"))
        await asyncio.sleep(0)
        assert task1.done() is False
        assert vmc.maintenance_hold.runs_in_flight == 1

        with pytest.raises(RuntimeError, match="already in progress"):
            await asyncio.wait_for(vmc.run_test_sale("WATER-1"), timeout=5)
        assert task1.done() is False
        assert vmc.maintenance_hold.runs_in_flight == 1

        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        assert vmc.maintenance_hold.runs_in_flight == 0
        assert vmc._test_sale_in_progress is False
        # Cancellation still clears escrow directly (never a real refund
        # command) and the per-sale flags, same as any other exit path.
        assert vmc.credit_escrow == 0.0
        assert vmc._sale_is_test is False

        released = vmc.end_maintenance("sess-1")
        assert released is True
        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        await asyncio.sleep(0)
        assert avail.payment_enabled is True

    async def test_run_raising_before_await_still_clears_guard_for_next_call(self):
        """Extra guard-safety coverage beyond rule 3's minimum: a plain
        raised `RuntimeError` from a DIFFERENT `run_test_sale` code path
        than the guard's own refusal -- the pre-existing "could not
        select" refusal (locked out), reached before `await waiter` is
        ever created -- still runs the new outer `finally` and clears
        `_test_sale_in_progress`. Proven by immediately making a second,
        real call for a different product and driving it to completion:
        if the guard had leaked, that second call would be incorrectly
        refused too, even though the first is fully finished and the
        calls do not overlap at all.
        """
        vmc, rec, client, avail = _test_run_vmc_with_availability()
        granted, _ = vmc.begin_maintenance("user-1", "sess-1")
        assert granted is True
        vmc._lockouts["ICE-1"] = FaultCode.ICE_401  # forces "could not select"

        with pytest.raises(RuntimeError, match="could not select"):
            await vmc.run_test_sale("ICE-1")

        assert vmc._test_sale_in_progress is False
        assert vmc.maintenance_hold.runs_in_flight == 0

        task2 = asyncio.get_running_loop().create_task(vmc.run_test_sale("WATER-1"))
        await asyncio.sleep(0)
        assert vmc.state == "interacting_with_user"
        vmc._process_payment()
        assert vmc.state == "dispensing"
        await vmc._handle_mqtt_dispenser(
            "hardware/dispenser", {"slot": 1, "state": "complete"}
        )
        result = await asyncio.wait_for(task2, timeout=5)
        assert result.sku == "WATER-1"
        assert result.outcome == "dispensed"
        assert vmc.maintenance_hold.runs_in_flight == 0

        released = vmc.end_maintenance("sess-1")
        assert released is True
        assert vmc.maintenance_hold is None
        assert "SVC-102" not in [f["code"] for f in vmc.active_faults()]
        await asyncio.sleep(0)
        assert avail.payment_enabled is True
