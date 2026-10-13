"""Tests for controller/sale_journal.py — one method per sale event."""

import pytest
from loguru import logger

from config.config_model import Product
from contracts.vending_machine import FaultCode
from controller.sale_journal import STATE_CHANGE_PREFIX, SaleJournal


class Sink:
    """Captures loguru records with their bound extras and level."""

    def __init__(self, trace: list):
        self.records: list[tuple[str, str, dict]] = []  # (level, message, extra)
        self._trace = trace
        self._id = logger.add(self._write, level="DEBUG")

    def _write(self, msg):
        r = msg.record
        extra = dict(r["extra"])
        self.records.append((r["level"].name, r["message"], extra))
        name = (
            "transaction"
            if extra.get("transaction")
            else "vending"
            if extra.get("vending")
            else "logger"
        )
        self._trace.append(("log", r["level"].name, name, r["message"]))

    def close(self):
        logger.remove(self._id)

    def lines(self, *, sink: str | None = None) -> list[tuple[str, str]]:
        """(level, message) for the plain logger (sink=None) or a bound sink."""
        out = []
        for level, message, extra in self.records:
            if (
                sink is None
                and not extra.get("transaction")
                and not extra.get("vending")
            ):
                out.append((level, message))
            elif sink is not None and extra.get(sink):
                out.append((level, message))
        return out


class Recorder:
    def __init__(self, trace: list | None = None):
        self.rows = []
        self._trace = trace if trace is not None else []

    def record(self, kind, *, value=None, metadata=None):
        self.rows.append((kind, value, metadata))
        self._trace.append(("record", kind, value, metadata))


class Messages(list):
    """Customer messages; every append also lands on the shared trace."""

    def __init__(self, trace: list):
        super().__init__()
        self._trace = trace

    def append(self, text):
        super().append(text)
        self._trace.append(("message", text))


@pytest.fixture
def trace():
    """One list every sink appends to, in call order."""
    return []


@pytest.fixture
def sink(trace):
    s = Sink(trace)
    yield s
    s.close()


@pytest.fixture
def harness(sink, trace):
    recorder = Recorder(trace)
    messages = Messages(trace)
    journal = SaleJournal(recorder=lambda: recorder, message=messages.append)
    return journal, recorder, messages, sink


PRODUCT = Product(name="Ice", sku="ICE", price=2.5, slot=1)
P = STATE_CHANGE_PREFIX


def test_payment_received_goes_to_logger_and_transaction_log(harness):
    journal, _, messages, sink = harness
    journal.payment_received(2.5, "card")
    assert sink.lines() == [("INFO", "MQTT payment received: $2.50 via card")]
    assert sink.lines(sink="transaction") == [
        ("INFO", "PAYMENT RECEIVED: $2.50 via card")
    ]
    assert sink.lines(sink="vending") == []
    assert messages == []


def test_product_selected_fans_out_to_all_three_sinks(harness):
    journal, _, _, sink = harness
    journal.product_selected(PRODUCT, 0)
    assert sink.lines() == [("INFO", "Selected product: Ice at $2.50")]
    assert sink.lines(sink="transaction") == [
        ("INFO", "PRODUCT SELECTED: 'Ice' ($2.50), button 0")
    ]
    assert sink.lines(sink="vending") == [
        ("INFO", "PRODUCT SELECTED: 'Ice' ($2.50), button 0")
    ]


def test_locked_out_logs_then_messages(harness):
    journal, _, messages, sink = harness
    journal.locked_out(PRODUCT, FaultCode.PAY_102)
    assert sink.lines(sink="transaction") == [
        ("INFO", "LOCKED OUT: 'Ice' (PAY-102), customer rejected")
    ]
    assert messages == ["Ice is unavailable (PAY-102). Please choose another product."]


def test_vend_failed_records_a_row_for_a_real_sale_only(harness):
    journal, recorder, messages, sink = harness
    journal.vend_failed("Ice", FaultCode.PAY_102, "jam", 2.5, "ICE", is_test=False)
    assert recorder.rows == [
        ("vend_failed", 2.5, {"code": "PAY-102", "sku": "ICE", "outcome": "jam"})
    ]
    assert sink.lines()[0] == (
        "ERROR",
        f"{P} Vend failed for 'Ice' (PAY-102, jam); $2.50 returned to escrow",
    )
    assert sink.lines(sink="transaction") == [
        ("ERROR", "VEND FAILED: 'Ice' PAY-102 (jam); $2.50 returned to escrow")
    ]
    assert messages == [
        "Sorry, Ice could not be dispensed (PAY-102). Your $2.50 credit has been kept."
    ]
    recorder.rows.clear()
    journal.vend_failed("Ice", FaultCode.PAY_102, "jam", 2.5, "ICE", is_test=True)
    assert recorder.rows == []


def test_recorder_is_read_at_call_time_and_none_skips_the_row(sink):
    holder = {"r": None}
    journal = SaleJournal(recorder=lambda: holder["r"], message=lambda _t: None)
    journal.error_entered(None)  # no recorder yet: must not raise
    holder["r"] = Recorder()
    journal.error_entered(None)
    assert holder["r"].rows == [("error", 1.0, None)]


def test_session_timeout_wording_per_state(harness):
    journal, _, _, sink = harness
    journal.session_timeout(1.0, "idle")
    journal.session_timeout(1.0, "error")
    journal.session_timeout(1.0, "interacting_with_user")
    assert sink.lines(sink="transaction") == [
        ("INFO", "SESSION TIMEOUT: refunding $1.00 (idle, no active selection)"),
        (
            "INFO",
            "SESSION TIMEOUT: refunding $1.00 (error, deposited after on_error's own refund)",
        ),
        ("INFO", "SESSION TIMEOUT: refunding $1.00"),
    ]
    assert sink.lines() == [
        ("INFO", "Idle session timed out with stranded escrow; refunding $1.00."),
        (
            "INFO",
            "Session timed out in error state with stranded escrow; refunding $1.00.",
        ),
        ("INFO", "Customer session timed out due to inactivity."),
    ]


def test_refund_failed_row_and_message(harness):
    journal, recorder, messages, sink = harness
    journal.refund_failed("abcdef0123", "error", 3.0, "nack")
    assert recorder.rows == [
        (
            "refund_failed",
            3.0,
            {"request_id": "abcdef0123", "reason": "error", "detail": "nack"},
        )
    ]
    assert messages == [
        "We could not return $3.00 automatically. Please contact support and quote abcdef01."
    ]
    assert sink.lines(sink="transaction") == [
        ("ERROR", "REFUND FAILED: $3.00 request_id=abcdef0123 reason=error detail=nack")
    ]


def test_button_press_three_sinks(harness):
    journal, _, _, sink = harness
    journal.button_press(2)
    assert sink.lines() == [("INFO", "MQTT button press: button 2")]
    assert sink.lines(sink="transaction") == [("INFO", "BUTTON PRESS: button 2")]
    assert sink.lines(sink="vending") == [("INFO", "BUTTON PRESS: button 2")]


def test_deposit_logs(harness):
    journal, _, messages, sink = harness
    journal.deposit_ignored(-1)
    journal.deposit_during_maintenance(1.0)
    journal.deposit_while_payment_disabled(1.0, ["a", "b"])
    journal.deposited(1.0, "cash", 3.5)
    assert sink.lines() == [
        ("WARNING", "Ignoring non-positive deposit: -1"),
        (
            "WARNING",
            "Credit $1.00 arrived during a maintenance lease; refunding rather than escrowing",
        ),
        ("WARNING", "Credit $1.00 arrived while payment is disabled (a, b); escrowed"),
        ("INFO", "Deposited $1.00 via cash. New escrow: $3.50"),
    ]
    assert messages == []


def test_selection_refusals(harness):
    journal, _, _, sink = harness
    journal.selection_refused_not_ready()
    journal.selection_refused_bad_index(7)
    assert sink.lines() == [
        ("WARNING", "Cannot change selection; machine not ready."),
        ("ERROR", "Invalid product index: 7"),
    ]


def test_unavailable(harness):
    journal, _, messages, sink = harness
    journal.unavailable(PRODUCT, "no_water")
    assert sink.lines(sink="transaction") == [
        ("INFO", "UNAVAILABLE: 'Ice' blocked by no_water, customer rejected")
    ]
    assert messages == [
        "Ice is unavailable right now (no_water). Please try again later."
    ]


def test_sold_out(harness):
    journal, _, messages, sink = harness
    journal.sold_out(PRODUCT)
    assert sink.lines() == [("ERROR", "Ice is sold out.")]
    assert sink.lines(sink="transaction") == [
        ("INFO", "SOLD OUT: 'Ice', customer rejected")
    ]
    assert messages == ["Ice is sold out. Please select another product."]


def test_payment_sufficient(harness):
    journal, _, messages, sink = harness
    journal.payment_sufficient(3.0, 2.5, PRODUCT)
    assert sink.lines() == [
        ("INFO", f"{P} Escrow sufficient (3.00 >= 2.50). Processing payment.")
    ]
    assert sink.lines(sink="transaction") == [
        ("INFO", "PAYMENT SUFFICIENT: $3.00 >= $2.50 for 'Ice', charging $2.50")
    ]
    assert messages == ["Sufficient funds received. Processing your payment..."]


def test_payment_insufficient(harness):
    journal, _, messages, sink = harness
    text = "Insufficient funds. Please insert an additional $1.50."
    journal.payment_insufficient(1.0, 2.5, PRODUCT, 1.5, text)
    assert sink.lines() == [("INFO", text)]
    assert sink.lines(sink="transaction") == [
        ("INFO", "PAYMENT INSUFFICIENT: $1.00 < $2.50 for 'Ice', need $1.50 more")
    ]
    assert messages == [text]


def test_catalog_lost_selection(harness):
    journal, _, _, sink = harness
    journal.catalog_lost_selection()
    assert sink.lines() == [
        ("ERROR", "Selected product no longer exists in the catalog; cancelling sale.")
    ]


def test_dispenser_logs(harness):
    journal, _, _, sink = harness
    journal.dispenser_step(1, "filling")
    journal.dispenser_outcome_ignored("jam", "idle", 1)
    journal.dispenser_outcome_without_cycle("jam", 1)
    assert sink.lines(sink="vending") == [("INFO", "DISPENSER: slot 1, state: filling")]
    assert sink.lines() == [
        (
            "WARNING",
            "Ignoring dispenser outcome 'jam' outside dispensing state (current state: idle, slot 1)",
        ),
        (
            "WARNING",
            "Ignoring dispenser outcome 'jam' with no active dispense cycle (slot 1)",
        ),
    ]


def test_dispense_success(harness):
    journal, _, _, sink = harness
    journal.dispense_success(1, "Ice")
    assert sink.lines(sink="transaction") == [
        ("INFO", "DISPENSE SUCCESS: slot 1, product 'Ice'")
    ]
    assert sink.lines(sink="vending") == [
        ("INFO", "DISPENSE COMPLETE: slot 1, product 'Ice'")
    ]
    assert sink.lines() == []


def test_dispense_failed(harness):
    journal, _, _, sink = harness
    journal.dispense_failed(1, "Ice", "jam", FaultCode.PAY_102)
    text = "DISPENSE FAILED: slot 1, product 'Ice', outcome: jam, fault: PAY-102"
    assert sink.lines(sink="transaction") == [("ERROR", text)]
    assert sink.lines(sink="vending") == [("ERROR", text)]
    assert sink.lines() == []


def test_late_failure_and_settled(harness):
    journal, _, _, sink = harness
    journal.late_failure_ignored(FaultCode.PAY_102, "no_report")
    journal.failure_after_settled()
    assert sink.lines() == [
        (
            "WARNING",
            "late dispatch failure for a cycle that is no longer current ignored (code=PAY-102, outcome='no_report')",
        ),
        (
            "DEBUG",
            "Dispense failed after the sale already left 'dispensing'; ignoring (not double-failing a settled sale).",
        ),
    ]


def test_shares_missing_and_test_credit_cleared_and_no_sellable(harness):
    journal, _, _, sink = harness
    journal.shares_missing(2.5)
    journal.test_credit_cleared(2.5, FaultCode.PAY_102, "jam")
    journal.no_sellable_products()
    assert sink.lines() == [
        (
            "WARNING",
            "on_vend_failed: no pending_sale_shares recorded; crediting $2.50 back to escrow as 'unknown'",
        ),
        ("DEBUG", "test credit cleared directly, never refunded ($2.50, PAY-102, jam)"),
    ]
    assert sink.lines(sink="transaction") == [
        ("INFO", "No sellable products remain; refunding and returning to idle")
    ]


def test_error_entered(harness):
    journal, recorder, _, sink = harness
    journal.error_entered("Ice")
    assert sink.lines() == [
        (
            "ERROR",
            f"{P} Error encountered for product: Ice. Transitioning to error state.",
        )
    ]
    assert recorder.rows == [("error", 1.0, None)]


def test_refund_requested(harness):
    journal, _, messages, sink = harness
    journal.refund_requested(2.0, "cash", "maintenance", "rid1")
    assert sink.lines() == [
        (
            "INFO",
            "Refund of $2.00 requested via cash (reason=maintenance, request_id=rid1)",
        )
    ]
    assert sink.lines(sink="transaction") == [
        ("INFO", "REFUND REQUESTED: $2.00 via cash reason=maintenance request_id=rid1")
    ]
    assert messages == ["Refund of $2.00 requested via cash. Please wait..."]


def test_refund_confirmed(harness):
    journal, recorder, messages, sink = harness
    journal.refund_confirmed("rid1", "maintenance", 2.0, "cash")
    assert sink.lines(sink="transaction") == [
        ("INFO", "REFUND CONFIRMED: $2.00 request_id=rid1")
    ]
    assert recorder.rows == [
        ("refund", 2.0, {"request_id": "rid1", "reason": "maintenance"})
    ]
    assert messages == ["Refund of $2.00 issued via cash."]


def test_lifecycle_lines(harness):
    journal, _, _, sink = harness
    journal.sale_cancelled("Ice")
    journal.entering_interaction("Ice")
    journal.entering_dispensing("Ice")
    journal.completing(0.5)
    journal.resetting("Ice")
    journal.finished_dispensing("Ice")
    journal.inventory_updated("Ice", 4)
    assert sink.lines() == [
        (
            "INFO",
            f"{P} Cancelling sale for product: Ice. Returning to idle without entering the error state.",
        ),
        ("INFO", f"{P} Transitioning to interacting_with_user for product: Ice"),
        ("INFO", f"{P} Transitioning to dispensing for product: Ice"),
        ("INFO", f"{P} Completing transaction. Remaining escrow: $0.50"),
        ("INFO", f"{P} Resetting to idle state. Previous selection: Ice"),
        ("INFO", f"{P} Finished dispensing: Ice"),
        ("INFO", "Inventory for Ice updated: 4 remaining."),
    ]
    assert sink.lines(sink="transaction") == [
        ("INFO", "SALE CANCELLED: selected product removed from catalog")
    ]


# -- cross-sink ordering ----------------------------------------------------
# Each expectation is the order controller/vmc.py wrote these effects in before
# the journal existed: plain logger, then transaction/vending sinks, then the
# recorder row, then the customer message.


@pytest.mark.parametrize("is_test", [False, True])
def test_trace_vend_failed(harness, trace, is_test):
    journal, _, _, _ = harness
    journal.vend_failed("Ice", FaultCode.PAY_102, "jam", 2.5, "ICE", is_test=is_test)
    expected = [
        (
            "log",
            "ERROR",
            "logger",
            f"{P} Vend failed for 'Ice' (PAY-102, jam); $2.50 returned to escrow",
        ),
        (
            "log",
            "ERROR",
            "transaction",
            "VEND FAILED: 'Ice' PAY-102 (jam); $2.50 returned to escrow",
        ),
    ]
    if not is_test:
        expected.append(
            (
                "record",
                "vend_failed",
                2.5,
                {"code": "PAY-102", "sku": "ICE", "outcome": "jam"},
            )
        )
    expected.append(
        (
            "message",
            "Sorry, Ice could not be dispensed (PAY-102). "
            "Your $2.50 credit has been kept.",
        )
    )
    assert trace == expected


def test_trace_refund_requested(harness, trace):
    journal, _, _, _ = harness
    journal.refund_requested(1.0, "card", "customer", "req-1")
    assert trace == [
        (
            "log",
            "INFO",
            "logger",
            "Refund of $1.00 requested via card (reason=customer, request_id=req-1)",
        ),
        (
            "log",
            "INFO",
            "transaction",
            "REFUND REQUESTED: $1.00 via card reason=customer request_id=req-1",
        ),
        ("message", "Refund of $1.00 requested via card. Please wait..."),
    ]


def test_trace_refund_confirmed(harness, trace):
    journal, _, _, _ = harness
    journal.refund_confirmed("req-1", "customer", 1.0, "card")
    assert trace == [
        ("log", "INFO", "transaction", "REFUND CONFIRMED: $1.00 request_id=req-1"),
        (
            "record",
            "refund",
            1.0,
            {"request_id": "req-1", "reason": "customer"},
        ),
        ("message", "Refund of $1.00 issued via card."),
    ]


def test_trace_refund_failed(harness, trace):
    journal, _, _, _ = harness
    journal.refund_failed("req-12345678", "customer", 1.0, "no_ack")
    assert trace == [
        (
            "log",
            "ERROR",
            "transaction",
            "REFUND FAILED: $1.00 request_id=req-12345678 "
            "reason=customer detail=no_ack",
        ),
        (
            "record",
            "refund_failed",
            1.0,
            {"request_id": "req-12345678", "reason": "customer", "detail": "no_ack"},
        ),
        (
            "message",
            "We could not return $1.00 automatically. "
            "Please contact support and quote req-1234.",
        ),
    ]


def test_trace_locked_out(harness, trace):
    journal, _, _, _ = harness
    journal.locked_out(PRODUCT, FaultCode.PAY_102)
    assert trace == [
        (
            "log",
            "INFO",
            "transaction",
            "LOCKED OUT: 'Ice' (PAY-102), customer rejected",
        ),
        (
            "message",
            "Ice is unavailable (PAY-102). Please choose another product.",
        ),
    ]


def test_trace_unavailable(harness, trace):
    journal, _, _, _ = harness
    journal.unavailable(PRODUCT, "no_water")
    assert trace == [
        (
            "log",
            "INFO",
            "transaction",
            "UNAVAILABLE: 'Ice' blocked by no_water, customer rejected",
        ),
        (
            "message",
            "Ice is unavailable right now (no_water). Please try again later.",
        ),
    ]


def test_trace_sold_out(harness, trace):
    journal, _, _, _ = harness
    journal.sold_out(PRODUCT)
    assert trace == [
        ("log", "ERROR", "logger", "Ice is sold out."),
        ("log", "INFO", "transaction", "SOLD OUT: 'Ice', customer rejected"),
        ("message", "Ice is sold out. Please select another product."),
    ]


def test_trace_payment_sufficient(harness, trace):
    journal, _, _, _ = harness
    journal.payment_sufficient(3.0, 2.5, PRODUCT)
    assert trace == [
        (
            "log",
            "INFO",
            "logger",
            f"{P} Escrow sufficient (3.00 >= 2.50). Processing payment.",
        ),
        (
            "log",
            "INFO",
            "transaction",
            "PAYMENT SUFFICIENT: $3.00 >= $2.50 for 'Ice', charging $2.50",
        ),
        ("message", "Sufficient funds received. Processing your payment..."),
    ]


def test_trace_payment_insufficient(harness, trace):
    journal, _, _, _ = harness
    text = "Insufficient funds. Please insert an additional $1.50."
    journal.payment_insufficient(1.0, 2.5, PRODUCT, 1.5, text)
    assert trace == [
        ("log", "INFO", "logger", text),
        (
            "log",
            "INFO",
            "transaction",
            "PAYMENT INSUFFICIENT: $1.00 < $2.50 for 'Ice', need $1.50 more",
        ),
        ("message", text),
    ]


def test_trace_error_entered(harness, trace):
    journal, _, _, _ = harness
    journal.error_entered("Ice")
    assert trace == [
        (
            "log",
            "ERROR",
            "logger",
            f"{P} Error encountered for product: Ice. Transitioning to error state.",
        ),
        ("record", "error", 1.0, None),
    ]
