"""The sale FSM's domain-event writer: the transaction/vending bound-sink log
lines and their paired plain-logger lines, the event-recorder rows and the
customer messages that accompany them, one method per sale event. Incidental
debug tracing and the MQTT dispenser-event line stay at the call site.
The strings, sinks and levels here are the log contract; do not reword them.
Nothing in this module reads FSM state: callers pass in every value.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from ice_common.config_model import Product
from ice_contracts.money import cents_to_dollar_text, format_cents
from ice_contracts.board.vending_machine import FaultCode

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"


class SaleJournal:
    """Every domain-event log line, event-recorder row and adjacent customer
    message the sale FSM emits, one method per event. `recorder` is read at each call."""

    def __init__(
        self,
        *,
        recorder: Callable[[], object | None],
        message: Callable[[str], None],
    ) -> None:
        self._recorder = recorder
        self._message = message
        self._txn = logger.bind(transaction=True)
        self._vend = logger.bind(vending=True)

    def _record(self, kind: str, *, value=None, metadata=None) -> None:
        recorder = self._recorder()
        if recorder:
            if metadata is None:
                recorder.record(kind, value=value)
            else:
                recorder.record(kind, value=value, metadata=metadata)

    # -- payment / buttons -------------------------------------------------

    def payment_received(self, amount: int, method: str) -> None:
        logger.info(f"MQTT payment received: {format_cents(amount)} via {method}")
        self._txn.info(f"PAYMENT RECEIVED: {format_cents(amount)} via {method}")

    def payment_event(self, kind: str, fields: dict) -> None:
        logger.info(f"MDB payment event {kind}: {fields}")
        self._txn.info(f"PAYMENT EVENT {kind}: {fields}")

    def non_cash_refund(self, cents: int, reason) -> None:
        logger.error(
            f"Cannot refund {format_cents(cents)} of non-cash credit "
            f"(reason={reason}); the MDB board only pays out cash"
        )
        self._txn.error(
            f"REFUND IMPOSSIBLE: {format_cents(cents)} non-cash credit reason={reason}"
        )

    def button_press(self, button: str) -> None:
        logger.info(f"MQTT button press: button {button}")
        self._txn.info(f"BUTTON PRESS: button {button}")
        self._vend.info(f"BUTTON PRESS: button {button}")

    # -- deposits ----------------------------------------------------------

    def deposit_ignored(self, amount: int) -> None:
        logger.warning(f"Ignoring non-positive deposit: {amount}")

    def deposit_during_maintenance(self, amount: int) -> None:
        logger.warning(
            f"Credit {format_cents(amount)} arrived during a maintenance lease; "
            "refunding rather than escrowing"
        )

    def deposit_while_payment_disabled(self, amount: int, reasons: list[str]) -> None:
        logger.warning(
            f"Credit {format_cents(amount)} arrived while payment is disabled "
            f"({', '.join(reasons)}); escrowed"
        )

    def deposited(self, amount: int, method: str, total: int) -> None:
        logger.info(
            f"Deposited {format_cents(amount)} via {method}. New escrow: {format_cents(total)}"
        )

    # -- selection ---------------------------------------------------------

    def selection_refused_not_ready(self) -> None:
        logger.warning("Cannot change selection; machine not ready.")

    def selection_refused_unknown_button(self, button: str) -> None:
        logger.warning(f"Button '{button}' is not mapped to any product")

    def locked_out(self, product: Product, code: FaultCode) -> None:
        self._txn.info(
            f"LOCKED OUT: '{product.name}' ({code.value}), customer rejected"
        )
        self._message(
            f"{product.name} is unavailable ({code.value}). "
            "Please choose another product."
        )

    def unavailable(self, product: Product, reason: str) -> None:
        self._txn.info(
            f"UNAVAILABLE: '{product.name}' blocked by {reason}, customer rejected"
        )
        self._message(
            f"{product.name} is unavailable right now ({reason}). "
            "Please try again later."
        )

    def product_selected(self, product: Product) -> None:
        price = format_cents(product.price_cents)
        logger.info(f"Selected product: {product.name} at {price}")
        text = f"PRODUCT SELECTED: '{product.name}' ({price}), sku {product.sku}"
        self._txn.info(text)
        self._vend.info(text)

    def sold_out(self, product: Product) -> None:
        logger.error(f"{product.name} is sold out.")
        self._txn.info(f"SOLD OUT: '{product.name}', customer rejected")
        self._message(f"{product.name} is sold out. Please select another product.")

    # -- payment processing ------------------------------------------------

    def payment_sufficient(self, escrow: int, price: int, product: Product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Escrow sufficient "
            f"({cents_to_dollar_text(escrow)} >= {cents_to_dollar_text(price)}). Processing payment."
        )
        self._txn.info(
            f"PAYMENT SUFFICIENT: {format_cents(escrow)} >= {format_cents(price)} "
            f"for '{product.name}', charging {format_cents(price)}"
        )
        self._message("Sufficient funds received. Processing your payment...")

    def payment_insufficient(
        self,
        escrow: int,
        price: int,
        product: Product,
        required: int,
        text: str,
    ) -> None:
        logger.info(text)
        self._txn.info(
            f"PAYMENT INSUFFICIENT: {format_cents(escrow)} < {format_cents(price)} "
            f"for '{product.name}', need {format_cents(required)} more"
        )
        self._message(text)

    def catalog_lost_selection(self) -> None:
        logger.error(
            "Selected product no longer exists in the catalog; cancelling sale."
        )

    # -- dispenser ---------------------------------------------------------

    def dispenser_step(self, slot, state) -> None:
        self._vend.info(f"DISPENSER: slot {slot}, state: {state}")

    def dispenser_outcome_ignored(self, outcome, state, slot) -> None:
        logger.warning(
            f"Ignoring dispenser outcome '{outcome}' outside dispensing "
            f"state (current state: {state}, slot {slot})"
        )

    def dispenser_outcome_without_cycle(self, outcome, slot) -> None:
        logger.warning(
            f"Ignoring dispenser outcome '{outcome}' with no active "
            f"dispense cycle (slot {slot})"
        )

    def dispense_success(self, slot, product_name: str) -> None:
        self._txn.info(f"DISPENSE SUCCESS: slot {slot}, product '{product_name}'")
        self._vend.info(f"DISPENSE COMPLETE: slot {slot}, product '{product_name}'")

    def dispense_failed(
        self, slot, product_name: str, outcome, code: FaultCode
    ) -> None:
        text = (
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome}, fault: {code.value}"
        )
        self._txn.error(text)
        self._vend.error(text)

    def late_failure_ignored(self, code: FaultCode, outcome) -> None:
        logger.warning(
            f"late dispatch failure for a cycle that is no longer current "
            f"ignored (code={code.value}, outcome={outcome!r})"
        )

    def failure_after_settled(self) -> None:
        logger.debug(
            "Dispense failed after the sale already left 'dispensing'; "
            "ignoring (not double-failing a settled sale)."
        )

    # -- failed vend -------------------------------------------------------

    def vend_failed(
        self,
        name: str,
        code: FaultCode,
        outcome,
        price: int,
        sku,
        *,
        is_test: bool,
    ) -> None:
        logger.error(
            f"{STATE_CHANGE_PREFIX} Vend failed for '{name}' ({code.value}, {outcome}); "
            f"{format_cents(price)} returned to escrow"
        )
        self._txn.error(
            f"VEND FAILED: '{name}' {code.value} ({outcome}); {format_cents(price)} returned to escrow"
        )
        if not is_test:
            self._record(
                "vend_failed",
                value=price,
                metadata={"code": code.value, "sku": sku, "outcome": outcome},
            )
        self._message(
            f"Sorry, {name} could not be dispensed ({code.value}). "
            f"Your {format_cents(price)} credit has been kept."
        )

    def shares_missing(self, price: int) -> None:
        logger.warning(
            "on_vend_failed: no pending_sale_shares recorded; crediting "
            f"{format_cents(price)} back to escrow as 'unknown'"
        )

    def test_credit_cleared(self, amount: int, code: FaultCode, outcome) -> None:
        logger.debug(
            f"test credit cleared directly, never refunded ({format_cents(amount)}, "
            f"{code.value}, {outcome})"
        )

    def no_sellable_products(self) -> None:
        self._txn.info("No sellable products remain; refunding and returning to idle")

    def error_entered(self, product) -> None:
        logger.error(
            f"{STATE_CHANGE_PREFIX} Error encountered for product: {product}. "
            "Transitioning to error state."
        )
        self._record("error", value=1.0)

    def error_restored(self, total: int) -> None:
        logger.warning(
            f"Error state: {format_cents(total)} of consumed sale credit returned to escrow "
            "for refund; no sale recorded"
        )
        self._txn.warning(f"ERROR: {format_cents(total)} returned to escrow for refund")

    def error_during_sale_write(self, product) -> None:
        logger.warning(
            f"Error entered while the sale write for {product} was in flight; "
            "the product was dispensed, so its credit is not restored or refunded"
        )

    def sale_write_finished_in_state(self, state: str) -> None:
        logger.warning(
            f"Sale write finished after the FSM left dispensing (now {state}); "
            "settling as dispensed without a transition"
        )

    def error_message(self, had_credit: bool) -> None:
        self._message(
            "An error has occurred. A refund of your credit has been requested. "
            "Please contact support if it does not arrive."
            if had_credit
            else "An error has occurred. Please contact support."
        )

    def interaction_started(self) -> None:
        self._message("Interaction started. Please insert funds or select a product.")

    def dispensing_started(self) -> None:
        self._message("Processing your payment and dispensing your product...")

    def transaction_complete(self) -> None:
        self._message("Transaction complete. Thank you for your purchase!")

    def product_removed(self) -> None:
        self._message(
            "Sorry, the selected product is no longer available. "
            "Please make a new selection."
        )

    def test_credit_cleared_on_error(self, amount: int) -> None:
        logger.debug(
            f"test credit cleared on error, never refunded ({format_cents(amount)})"
        )

    # -- refunds -----------------------------------------------------------

    def refund_requested(self, amount: int, method, reason, request_id: str) -> None:
        logger.info(
            f"Refund of {format_cents(amount)} requested via {method} "
            f"(reason={reason}, request_id={request_id})"
        )
        self._txn.info(
            f"REFUND REQUESTED: {format_cents(amount)} via {method} "
            f"reason={reason} request_id={request_id}"
        )
        self._message(
            f"Refund of {format_cents(amount)} requested via {method}. Please wait..."
        )

    def refund_confirmed(
        self, request_id: str, reason, amount_returned_cents: int, method
    ) -> None:
        self._txn.info(
            f"REFUND CONFIRMED: {format_cents(amount_returned_cents)} "
            f"request_id={request_id}"
        )
        self._record(
            "refund",
            value=amount_returned_cents,
            metadata={"request_id": request_id, "reason": reason},
        )
        self._message(
            f"Refund of {format_cents(amount_returned_cents)} issued via {method}."
        )

    # -- session / lifecycle -----------------------------------------------

    def session_timeout(self, amount: int, state: str) -> None:
        if state == "idle":
            logger.info(
                f"Idle session timed out with stranded escrow; refunding {format_cents(amount)}."
            )
            self._txn.info(
                f"SESSION TIMEOUT: refunding {format_cents(amount)} (idle, no active selection)"
            )
        elif state == "error":
            logger.info(
                "Session timed out in error state with stranded escrow; "
                f"refunding {format_cents(amount)}."
            )
            self._txn.info(
                f"SESSION TIMEOUT: refunding {format_cents(amount)} "
                "(error, deposited after on_error's own refund)"
            )
        else:
            logger.info("Customer session timed out due to inactivity.")
            self._txn.info(f"SESSION TIMEOUT: refunding {format_cents(amount)}")

    def sale_cancelled(self, product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Cancelling sale for product: {product}. "
            "Returning to idle without entering the error state."
        )
        self._txn.info("SALE CANCELLED: selected product removed from catalog")

    def entering_interaction(self, product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to interacting_with_user "
            f"for product: {product}"
        )

    def entering_dispensing(self, product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to dispensing for product: {product}"
        )

    def completing(self, escrow: int) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Completing transaction. "
            f"Remaining escrow: {format_cents(escrow)}"
        )

    def resetting(self, product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Resetting to idle state. "
            f"Previous selection: {product}"
        )

    def finished_dispensing(self, product_name) -> None:
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {product_name}")

    def inventory_updated(self, name: str, remaining) -> None:
        logger.info(f"Inventory for {name} updated: {remaining} remaining.")
