"""The sale FSM's single writer of log lines, event-recorder rows and the
customer messages that accompany them, one method per sale event.
The strings, sinks and levels here are the log contract; do not reword them.
Nothing in this module reads FSM state: callers pass in every value.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from config.config_model import Product
from contracts.vending_machine import FaultCode

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"


class SaleJournal:
    """Every log line, event-recorder row and adjacent customer message the
    sale FSM emits, one method per event. `recorder` is read at each call."""

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

    def payment_received(self, amount: float, method: str) -> None:
        logger.info(f"MQTT payment received: ${amount:.2f} via {method}")
        self._txn.info(f"PAYMENT RECEIVED: ${amount:.2f} via {method}")

    def button_press(self, button) -> None:
        logger.info(f"MQTT button press: button {button}")
        self._txn.info(f"BUTTON PRESS: button {button}")
        self._vend.info(f"BUTTON PRESS: button {button}")

    # -- deposits ----------------------------------------------------------

    def deposit_ignored(self, amount) -> None:
        logger.warning(f"Ignoring non-positive deposit: {amount}")

    def deposit_during_maintenance(self, amount: float) -> None:
        logger.warning(
            f"Credit ${amount:.2f} arrived during a maintenance lease; "
            "refunding rather than escrowing"
        )

    def deposit_while_payment_disabled(self, amount: float, reasons: list[str]) -> None:
        logger.warning(
            f"Credit ${amount:.2f} arrived while payment is disabled "
            f"({', '.join(reasons)}); escrowed"
        )

    def deposited(self, amount: float, method: str, total: float) -> None:
        logger.info(f"Deposited ${amount:.2f} via {method}. New escrow: ${total:.2f}")

    # -- selection ---------------------------------------------------------

    def selection_refused_not_ready(self) -> None:
        logger.warning("Cannot change selection; machine not ready.")

    def selection_refused_bad_index(self, index) -> None:
        logger.error(f"Invalid product index: {index}")

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

    def product_selected(self, product: Product, button) -> None:
        logger.info(f"Selected product: {product.name} at ${product.price:.2f}")
        text = (
            f"PRODUCT SELECTED: '{product.name}' (${product.price:.2f}), "
            f"button {button}"
        )
        self._txn.info(text)
        self._vend.info(text)

    def sold_out(self, product: Product) -> None:
        logger.error(f"{product.name} is sold out.")
        self._txn.info(f"SOLD OUT: '{product.name}', customer rejected")
        self._message(f"{product.name} is sold out. Please select another product.")

    # -- payment processing ------------------------------------------------

    def payment_sufficient(self, escrow: float, price: float, product: Product) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Escrow sufficient "
            f"({escrow:.2f} >= {price:.2f}). Processing payment."
        )
        self._txn.info(
            f"PAYMENT SUFFICIENT: ${escrow:.2f} >= ${price:.2f} "
            f"for '{product.name}', charging ${price:.2f}"
        )
        self._message("Sufficient funds received. Processing your payment...")

    def payment_insufficient(
        self,
        escrow: float,
        price: float,
        product: Product,
        required: float,
        text: str,
    ) -> None:
        logger.info(text)
        self._txn.info(
            f"PAYMENT INSUFFICIENT: ${escrow:.2f} < ${price:.2f} "
            f"for '{product.name}', need ${required:.2f} more"
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
        price: float,
        sku,
        *,
        is_test: bool,
    ) -> None:
        logger.error(
            f"{STATE_CHANGE_PREFIX} Vend failed for '{name}' ({code.value}, {outcome}); "
            f"${price:.2f} returned to escrow"
        )
        self._txn.error(
            f"VEND FAILED: '{name}' {code.value} ({outcome}); ${price:.2f} returned to escrow"
        )
        if not is_test:
            self._record(
                "vend_failed",
                value=price,
                metadata={"code": code.value, "sku": sku, "outcome": outcome},
            )
        self._message(
            f"Sorry, {name} could not be dispensed ({code.value}). "
            f"Your ${price:.2f} credit has been kept."
        )

    def shares_missing(self, price: float) -> None:
        logger.warning(
            "on_vend_failed: no pending_sale_shares recorded; crediting "
            f"${price:.2f} back to escrow as 'unknown'"
        )

    def test_credit_cleared(self, amount: float, code: FaultCode, outcome) -> None:
        logger.debug(
            f"test credit cleared directly, never refunded (${amount:.2f}, "
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

    # -- refunds -----------------------------------------------------------

    def refund_requested(self, amount: float, method, reason, request_id: str) -> None:
        logger.info(
            f"Refund of ${amount:.2f} requested via {method} "
            f"(reason={reason}, request_id={request_id})"
        )
        self._txn.info(
            f"REFUND REQUESTED: ${amount:.2f} via {method} "
            f"reason={reason} request_id={request_id}"
        )
        self._message(f"Refund of ${amount:.2f} requested via {method}. Please wait...")

    def refund_confirmed(
        self, request_id: str, reason, amount_returned: float, method
    ) -> None:
        self._txn.info(
            f"REFUND CONFIRMED: ${amount_returned:.2f} request_id={request_id}"
        )
        self._record(
            "refund",
            value=amount_returned,
            metadata={"request_id": request_id, "reason": reason},
        )
        self._message(f"Refund of ${amount_returned:.2f} issued via {method}.")

    def refund_failed(self, request_id: str, reason, amount: float, detail) -> None:
        self._txn.error(
            f"REFUND FAILED: ${amount:.2f} request_id={request_id} "
            f"reason={reason} detail={detail}"
        )
        self._record(
            "refund_failed",
            value=amount,
            metadata={"request_id": request_id, "reason": reason, "detail": detail},
        )
        self._message(
            f"We could not return ${amount:.2f} automatically. "
            f"Please contact support and quote {request_id[:8]}."
        )

    # -- session / lifecycle -----------------------------------------------

    def session_timeout(self, amount: float, state: str) -> None:
        if state == "idle":
            logger.info(
                f"Idle session timed out with stranded escrow; refunding ${amount:.2f}."
            )
            self._txn.info(
                f"SESSION TIMEOUT: refunding ${amount:.2f} (idle, no active selection)"
            )
        elif state == "error":
            logger.info(
                "Session timed out in error state with stranded escrow; "
                f"refunding ${amount:.2f}."
            )
            self._txn.info(
                f"SESSION TIMEOUT: refunding ${amount:.2f} "
                "(error, deposited after on_error's own refund)"
            )
        else:
            logger.info("Customer session timed out due to inactivity.")
            self._txn.info(f"SESSION TIMEOUT: refunding ${amount:.2f}")

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

    def completing(self, escrow: float) -> None:
        logger.info(
            f"{STATE_CHANGE_PREFIX} Completing transaction. "
            f"Remaining escrow: ${escrow:.2f}"
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
