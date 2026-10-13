# controller/vmc.py
import asyncio
import time
from collections.abc import Callable
from loguru import logger
from services.payment_gateway_manager import PaymentGatewayManager
from services.mqtt_messages import (
    PaymentEvent,
    ButtonPress,
)
from contracts.vending_machine import (
    DispenserOutcome,
    FaultCode,
    PaymentRefundResult,
)
from config.config_model import ConfigModel, Product
from services.availability import Availability
from services.inventory_manager import InventoryManager
from services.session_store import SessionSnapshot
from controller.dispense_cycle import DispenseCycle, DispenseReport
from controller.fault_service import FaultService
from controller.escrow_ledger import EscrowLedger
from controller.outputs import StatusOutputs
from controller.refund_protocol import PendingRefund, RefundProtocol
from controller.sale_context import SaleContext
from controller.session_timer import SessionTimer
from controller.task_runner import TaskRunner
from controller.dispenser_gate import DispenserProfileGate
from controller.sale_journal import STATE_CHANGE_PREFIX, SaleJournal  # noqa: F401  (tests import STATE_CHANGE_PREFIX from here)


class InvalidTransition(RuntimeError):
    """A trigger fired from a state it is not defined for."""


class VMC:
    """The sale finite-state machine: `idle`, `interacting_with_user`,
    `dispensing`, `error` (transitions are the explicit trigger methods below).

    Holds no transport or service handle: `Machine` injects every collaborator,
    and everything the FSM publishes, persists, displays or faults goes through
    `outputs` / `faults`. One `DispenseCycle` per entry into `dispensing` owns
    that sale's dispatch. Others observe via `subscribe_state_change` /
    `subscribe_sale_settled`.
    """

    states = ["idle", "interacting_with_user", "dispensing", "error"]

    REFUND_ACK_TIMEOUT = 10.0  # seconds to wait for cmd/payment/refund/ack
    REFUND_MAX_ATTEMPTS = 2  # one retry with the same request_id, then PAY-103

    # Amounts within half a cent are the same money (float noise). Equals
    # EscrowLedger.TOLERANCE; kept here because callers use VMC.CREDIT_TOLERANCE.
    CREDIT_TOLERANCE = EscrowLedger.TOLERANCE

    @logger.catch()
    def __init__(
        self,
        config: ConfigModel,
        *,
        tasks: TaskRunner,
        escrow: EscrowLedger,
        refunds: RefundProtocol,
        faults: FaultService,
        outputs: StatusOutputs,
        gate: DispenserProfileGate,
        in_maintenance: Callable[[], bool],
        availability: Callable[[], Availability | None],
        inventory: Callable[[], InventoryManager | None],
        dispense_factory: Callable[[SaleContext], DispenseCycle],
        journal: SaleJournal,
    ):
        """`Machine` (controller/machine.py) is the sole constructor and passes every
        collaborator in. `in_maintenance`, `availability` and `inventory`
        are zero-arg callables read at call time, since the live instances are
        attached after construction. The maintenance lease reaches the VMC only
        through the `in_maintenance` boolean.
        """
        self._journal = journal
        logger.debug("Initializing VMC with pre-loaded ConfigModel")

        self.config_model = config

        self.products = self.config_model.products

        # The in-flight sale; None when no sale is in progress.
        self._sale: SaleContext | None = None
        # credit_escrow must always equal the sum of escrow_credits.
        self._escrow = escrow
        # One DispenseCycle per entry into 'dispensing'; None when none is in flight.
        self._dispense_factory = dispense_factory
        self._cycle: DispenseCycle | None = None
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"

        self._tasks = tasks
        self._session_timer = SessionTimer(
            tasks, lambda: self.session_timeout_seconds, self._expire_session
        )
        self._inventory: Callable[[], InventoryManager | None] = inventory
        self._availability: Callable[[], Availability | None] = availability
        self._outputs = outputs
        self._in_maintenance: Callable[[], bool] = in_maintenance
        # True for exactly one run_test_sale call (double-submit guard).
        self._test_sale_in_progress: bool = False
        self._state_change_observers: list[Callable[[str], None]] = []
        self._sale_settled_observers: list[
            Callable[[SaleContext, str, str | None], None]
        ] = []
        self._faults = faults
        self._gate = gate
        self._refunds = refunds
        # Public so tests can override.
        self.session_timeout_seconds = 180.0  # 3 minutes
        self.dispense_timeout_seconds = (
            self.config_model.physical.dispense_timeout_seconds
        )

        self.state = VMC.states[0]
        logger.debug("FSM transitions set up successfully.")

        self.payment_gateway_manager = PaymentGatewayManager(
            config=self.config_model.payment.model_dump()
        )

        logger.debug("VMC initialization complete.")

    def cancel_timers(self) -> None:
        """Cancel the dispense cycle's timer and the session timeout (shutdown)."""
        if self._cycle is not None:
            self._cycle.cancel()
        self._session_timer.cancel()

    @property
    def outputs(self) -> StatusOutputs:
        """Read-only view of the FSM's outbound channel."""
        return self._outputs

    def snapshot(self, state: str | None = None) -> SessionSnapshot:
        """Build a `SessionSnapshot` of the current live state (pure, no side effects)."""
        return self._snapshot(state)

    def _snapshot(self, state: str | None = None) -> SessionSnapshot:
        pending = self._refunds.first_request_id()
        product = self.selected_product
        effective_state = state or self.state
        return SessionSnapshot(
            state=effective_state,
            credit_escrow=round(self.credit_escrow, 2),
            selected_sku=product.sku if product else None,
            dispense_slot=product.slot
            if product and effective_state == "dispensing"
            else None,
            dispense_mechanism=(self._sale.mechanism if self._sale else None)
            if effective_state == "dispensing"
            else None,
            dispense_started_at=time.time()
            if effective_state == "dispensing"
            else None,
            pending_refund_request_id=pending,
            credits=self._escrow.snapshot_credits(),
            pending_sale_shares=dict(self.pending_sale_shares)
            if self.pending_sale_shares is not None
            else None,
            # _sale is already None when a failed vend's snapshot is taken; the re-entrancy
            # flag keeps a test sale's snapshot is_test=True (else a crash there raises PAY-104).
            is_test=(
                self._sale.is_test
                if self._sale is not None
                else self._test_sale_in_progress
            ),
        )

    # --- Fault service (state and side effects live in FaultService) ---

    @property
    def faults(self) -> FaultService:
        """Read-only view of the fault service."""
        return self._faults

    # --- Escrow ledger ---
    # Properties over `self._escrow`; the setters replace `total` and `credits`
    # independently, so the divergence guard in `EscrowLedger.consume_fifo` still works.

    @property
    def credit_escrow(self) -> float:
        return self._escrow.total

    @credit_escrow.setter
    def credit_escrow(self, value: float) -> None:
        self._escrow.total = value

    @property
    def escrow_credits(self) -> list:
        return self._escrow.credits

    @escrow_credits.setter
    def escrow_credits(self, value: list) -> None:
        self._escrow.credits = value

    @property
    def escrow(self) -> EscrowLedger:
        """The escrow ledger itself."""
        return self._escrow

    @property
    def refunds(self) -> RefundProtocol:
        """Read-only view of the refund protocol."""
        return self._refunds

    # --- In-flight sale (SaleContext) ---
    # `self._sale` is replaced wholesale at each transition. `selected_product` is
    # read/write (tests assign it to set up a sale); `pending_sale_shares` is read-only.

    @property
    def sale(self) -> SaleContext | None:
        """The in-flight sale, or None."""
        return self._sale

    @property
    def selected_product(self) -> Product | None:
        return self._sale.product if self._sale is not None else None

    @selected_product.setter
    def selected_product(self, product: Product | None) -> None:
        if product is None:
            self._sale = None
            return
        if self._sale is None or self._sale.product is not product:
            self._sale = SaleContext(product=product, started_at=time.time())
        # Same product: keep the existing context (run_test_sale seeds is_test
        # before select_product assigns the product).

    @property
    def pending_sale_shares(self) -> dict[str, float] | None:
        return self._sale.shares if self._sale is not None else None

    @property
    def test_sale_in_progress(self) -> bool:
        """True while a simulated sale holds the re-entrancy guard."""
        return self._test_sale_in_progress

    def _sellable_products(self) -> list:
        return [p for p in self.products if self._faults.is_locked(p.sku) is None]

    def active_faults(self) -> list[dict]:
        """Snapshot for the dashboard/health monitor."""
        return self._faults.active_faults()

    def raise_fault(
        self,
        code: FaultCode,
        *,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record a fault (forwards to `FaultService`)."""
        self._faults.raise_fault(code, sku=sku, outcome=outcome)

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key (forwards to `FaultService`)."""
        return self._faults.clear_fault(key, by=by)

    # --- MQTT inbound handlers ---

    async def on_payment_credit(self, topic: str, data: dict):
        """Handle payment credit from MDB ESP32."""
        event = PaymentEvent.model_validate(data)
        self._journal.payment_received(event.amount, event.method)
        self.deposit_funds(event.amount, payment_method=event.method)

    async def on_button_press(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        self._journal.button_press(press.button)
        self.select_product(press.button)

    def note_dispense_request(self, request_id: str, mechanism: str) -> None:
        """Record the `request_id`/`mechanism` a `DispenseCycle` minted for the in-flight sale."""
        self._sale = self._sale.with_(mechanism=mechanism, request_id=request_id)

    async def on_dispense_failed(
        self, cycle: DispenseCycle, code: FaultCode, outcome: str
    ) -> None:
        """Fail an in-flight dispense; the `on_failed` callback of every `DispenseCycle`.

        Covers dispatch failures and the no-report timeout alike.
        """
        # Identity guard: a late failure from an earlier sale's dispatch must not
        # cancel, fault or refund the sale now dispensing. The state check covers a
        # sale that already settled through the hardware report.
        if cycle is not self._cycle:
            self._journal.late_failure_ignored(code, outcome)
            return
        if self.state != "dispensing":
            self._journal.failure_after_settled()
            return
        cycle.cancel()
        sku = self.selected_product.sku if self.selected_product else None
        self.raise_fault(code, sku=sku, outcome=outcome)
        # "timeout" stays its own outcome though it runs the same vend_failed path.
        self._settle(
            "timeout" if outcome == "no_report" else "vend_failed",
            None if outcome == "no_report" else code.value,
            lambda: self._fail_vend(code, outcome=outcome),
        )

    async def on_dispenser_event(self, topic: str, data: dict):
        """Handle dispenser status from ESP32.

        Only DispenserOutcome members end a sale; every other `state` string
        is an intermediate hardware step and is logged (inside
        `DispenseCycle.classify` once a cycle is active).
        """
        logger.info(f"MQTT dispenser event: {data}")
        state = data.get("state", "")
        slot = data.get("slot", "?")
        try:
            outcome = DispenserOutcome(state)
        except ValueError:
            self._journal.dispenser_step(slot, state)
            return

        if self.state != "dispensing":
            self._journal.dispenser_outcome_ignored(outcome.value, self.state, slot)
            return
        if self._cycle is None:
            self._journal.dispenser_outcome_without_cycle(outcome.value, slot)
            return

        report = self._cycle.classify(data)
        if report is None:
            return

        if report.success:
            await self._dispense_succeeded(report, slot)
        else:
            self._dispense_failed_report(report, slot)

    async def _dispense_succeeded(self, report: DispenseReport, slot: object) -> None:
        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        # Cancel the timeout before the await in record(): the customer already has
        # the product, so a timeout firing during that suspension must not fail it.
        self._cycle.cancel()
        self._journal.dispense_success(slot, product_name)
        if self._sale is not None and self._sale.is_test:
            # is_test lives on the sale, not the lease, so a lease release cannot flip it
            # to production; record() is skipped, so no sale/dispense rows are written.
            self._sale = self._sale.with_(shares=None)
        else:
            recorded = await self._cycle.record()
            if recorded and self._sale is not None:
                self._sale = self._sale.with_(shares=None)
            # A `False` return is the PAY-104 path: shares stay so the
            # on-disk session snapshot remains the sale's only record.
        if report.outcome is DispenserOutcome.door_open:
            # Customer success that is also a hardware fault (trap door did not close).
            sku = self.selected_product.sku if self.selected_product else None
            self.raise_fault(FaultCode.ICE_402, sku=sku, outcome=report.outcome.value)
        self._settle("dispensed", None, self._finish_dispensing)

    def _dispense_failed_report(self, report: DispenseReport, slot: object) -> None:
        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        self._cycle.cancel()
        code = report.fault
        sku = self.selected_product.sku if self.selected_product else None
        self._journal.dispense_failed(slot, product_name, report.outcome.value, code)
        self.raise_fault(code, sku=sku, outcome=report.outcome.value)
        self._settle(
            "vend_failed",
            code.value,
            lambda: self._fail_vend(code, outcome=report.outcome.value),
        )

    def _drop_sale(self) -> None:
        """Forget the in-flight sale: cancel its cycle, clear the selection and the insufficient-funds memo."""
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        self.selected_product = None
        self.last_insufficient_message = ""

    def _settle(
        self, outcome: str, fault_code: str | None, run: Callable[[], None]
    ) -> None:
        """Run the transition that clears `self._sale`, then notify observers.

        The sale is captured before `run` (its hook clears it); notification fires
        after, so observers see an already-settled FSM.
        """
        sale = self._sale
        run()
        if sale is not None:
            self._notify_settled(sale, outcome, fault_code)

    def _schedule(
        self, delay_seconds, callback, *, label: str = ""
    ) -> asyncio.Task | None:
        """Schedule a synchronous callback to run after delay_seconds on the event loop."""
        return self._tasks.schedule(delay_seconds, callback, label=label)

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "selected_product": self.selected_product.name
            if self.selected_product
            else None,
            "credit_escrow": self.credit_escrow,
            "last_payment_method": self.last_payment_method,
        }

    @logger.catch()
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        return self._escrow.has_credit

    @logger.catch()
    def send_customer_message(self, message):
        """Send a message to the customer via the registered callback."""
        self._outputs.message(message)

    # --- Observers ---

    def subscribe_state_change(self, cb: Callable[[str], None]) -> Callable[[], None]:
        """Subscribe to every FSM state transition; returns an unsubscribe closure."""
        self._state_change_observers.append(cb)

        def _unsubscribe() -> None:
            if cb in self._state_change_observers:
                self._state_change_observers.remove(cb)

        return _unsubscribe

    def subscribe_sale_settled(
        self, cb: Callable[[SaleContext, str, str | None], None]
    ) -> Callable[[], None]:
        """Subscribe to a sale settling: `(sale, outcome, fault_code)`, with `sale`
        captured before the transition clears it. Returns an unsubscribe closure."""
        self._sale_settled_observers.append(cb)

        def _unsubscribe() -> None:
            if cb in self._sale_settled_observers:
                self._sale_settled_observers.remove(cb)

        return _unsubscribe

    def _notify_settled(
        self, sale: SaleContext, outcome: str, fault_code: str | None
    ) -> None:
        """Run every sale-settled observer; one raising is logged, never stops the rest."""
        for cb in list(self._sale_settled_observers):
            try:
                cb(sale, outcome, fault_code)
            except Exception:
                logger.exception("sale-settled observer raised")

    # --- FSM triggers ---
    # idle --start_interaction--> interacting_with_user --dispense_product--> dispensing
    # dispensing --complete_transaction--> idle (no credit) or interacting_with_user
    # dispensing --vend_failed--> interacting_with_user (product locked out)
    # interacting_with_user --cancel_sale--> idle; any --error_occurred--> error --reset_state--> idle
    # Each hook runs before the state changes; _after_state_change runs after. `set_state`
    # bypasses both (`_fail_vend`, `_expire_session`: reset_state only fires from `error`).
    def _transition(
        self,
        source: str | None,
        dest: str,
        before: Callable[[], None],
    ) -> None:
        if source is not None and self.state != source:
            raise InvalidTransition(f"{dest!r} is not reachable from {self.state!r}")
        before()
        self.state = dest
        self._after_state_change()

    def set_state(self, state: str) -> None:
        """Force the state without running any hook or observer — the two
        in-FSM bypasses (`_fail_vend`, `_expire_session`) and tests."""
        if state not in self.states:
            raise ValueError(f"unknown state {state!r}")
        self.state = state

    def start_interaction(self) -> None:
        self._transition("idle", "interacting_with_user", self.on_start_interaction)

    def dispense_product(self) -> None:
        self._transition(
            "interacting_with_user", "dispensing", self.on_dispense_product
        )

    def complete_transaction(self) -> None:
        # Price was already deducted from escrow; leftover credit keeps the customer.
        dest = "interacting_with_user" if self.has_credit() else "idle"
        self._transition("dispensing", dest, self.on_complete_transaction)

    def cancel_sale(self) -> None:
        self._transition("interacting_with_user", "idle", self.on_cancel_sale)

    def vend_failed(self, *, code: FaultCode, outcome: str) -> None:
        self._transition(
            "dispensing",
            "interacting_with_user",
            lambda: self.on_vend_failed(code, outcome),
        )

    def error_occurred(self) -> None:
        # From any state, including `error` itself (re-runs on_error).
        self._transition(None, "error", self.on_error)

    def reset_state(self) -> None:
        self._transition("error", "idle", self.on_reset)

    # --- FSM Callback Methods ---
    def _after_state_change(self):
        """Runs after every FSM transition with self.state already updated."""
        self._outputs.state_changed(self.state)
        for cb in list(self._state_change_observers):
            try:
                cb(self.state)
            except Exception:
                logger.exception("state-change observer raised")

    @logger.catch()
    def on_start_interaction(self):
        self._journal.entering_interaction(self.selected_product)
        self._session_timer.arm()
        self._outputs.display("interacting_with_user")
        self._outputs.refresh()
        self.send_customer_message(
            "Interaction started. Please insert funds or select a product."
        )

    @logger.catch()
    def on_dispense_product(self):
        self._journal.entering_dispensing(self.selected_product)
        self._session_timer.cancel()
        self._outputs.display("dispensing")
        self._outputs.refresh()
        self.send_customer_message(
            "Processing your payment and dispensing your product..."
        )
        # Dispatch uses the product's own `slot`, not its list index: deleting an
        # earlier product shifts indices but not the physical slot.
        if self._tasks.loop and self.selected_product:
            self._cycle = self._dispense_factory(self._sale)
            self._cycle.start(
                lambda s: self._snapshot(s) if self._outputs.session_store else None
            )

    def _post_dispense_dest(self) -> str:
        """Return the FSM destination after dispensing: continue if credit remains, else idle."""
        return "interacting_with_user" if self.credit_escrow > 0 else "idle"

    @logger.catch()
    def on_complete_transaction(self):
        self._journal.completing(self.credit_escrow)
        dest = self._post_dispense_dest()
        # Clear the selection: a stale selection would let a late duplicate dispenser fault
        # refund at the old product's price.
        self.selected_product = None
        self._outputs.display(dest)
        self._outputs.refresh()
        if self.credit_escrow > 0:
            self.send_customer_message(
                "Transaction complete. You have remaining credit. Please select another product if desired."
            )
        else:
            self.send_customer_message(
                "Transaction complete. Thank you for your purchase!"
            )

    @logger.catch()
    def on_reset(self):
        self._journal.resetting(self.selected_product)
        self._drop_sale()
        self._outputs.display("idle")
        self._outputs.refresh()

    @logger.catch()
    def on_cancel_sale(self):
        """Cancel a live session whose selected product vanished from the catalog,
        without parking the machine in `error`."""
        self._journal.sale_cancelled(self.selected_product)
        self._session_timer.cancel()
        # Refund first: it reads only escrow, never the selection or cycle.
        self.request_refund(reason="cancel")
        self._drop_sale()
        self._outputs.display("idle")
        self._outputs.refresh()
        self.send_customer_message(
            "Sorry, the selected product is no longer available. Please make a new selection."
        )

    @logger.catch()
    def on_vend_failed(self, code: FaultCode, outcome: str):
        """`before` hook for dispensing -> interacting_with_user on a failed vend.

        Restores the price to escrow and clears the selection; `_fail_vend` then
        decides what happens next. Re-credits exactly the per-method shares consumed
        for this sale, as separate Credits, so a failure never reclassifies money.
        """
        product = self.selected_product
        name = product.name if product else "Unknown"
        sku = product.sku if product else None
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        shares = self.pending_sale_shares
        if self._sale is not None:
            self._sale = self._sale.with_(shares=None)
        if shares is None:
            # Unreachable guard: consume_fifo sets the shares before dispensing.
            price = product.price if product else 0.0
            self._journal.shares_missing(price)
            shares = {"unknown": round(price, 2)}
        else:
            # Price from the shares, not product.price: the live catalog object's
            # price can be edited mid-dispense.
            price = round(sum(shares.values()), 2)
        now = time.time()
        self._escrow.restore(shares, now, price)
        is_test = self._sale is not None and self._sale.is_test
        self.selected_product = None
        self.last_insufficient_message = ""
        self._journal.vend_failed(name, code, outcome, price, sku, is_test=is_test)

    def _fail_vend(self, code: FaultCode, outcome: str) -> None:
        """Run the vend_failed transition, then decide: choose again, or pay out."""
        # Captured BEFORE vend_failed: on_vend_failed clears self._sale.
        is_test = self._sale is not None and self._sale.is_test
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        self.vend_failed(code=code, outcome=outcome)
        if is_test:
            # on_vend_failed restored the synthetic "test" credit; clear it directly,
            # never via request_refund, whether or not run_test_sale is still waiting.
            cleared = self._escrow.take_all()
            self._journal.test_credit_cleared(cleared, code, outcome)
        if not self._sellable_products():
            self._journal.no_sellable_products()
            # Test money never leaves via a real refund; run_test_sale clears it.
            if not is_test:
                self.request_refund(reason=code.value)
            self._session_timer.cancel()
            self.set_state("idle")
            self._outputs.state_changed(self.state)
            self._outputs.display("idle")
        else:
            self.send_customer_message("Please choose another product.")
            self._session_timer.arm()
            self._outputs.state_changed(self.state)
            self._outputs.display("interacting_with_user")
        self._outputs.refresh()

    @logger.catch()
    def on_error(self):
        self._journal.error_entered(self.selected_product)
        # Partial clear (on_reset drops the whole sale): on_error keeps the selection,
        # and can fire from idle with no sale at all.
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        had_credit = self.credit_escrow > 0
        if had_credit:
            self.request_refund(reason="error")
        self._outputs.display("error")
        self._outputs.refresh()
        if had_credit:
            self.send_customer_message(
                "An error has occurred. A refund of your credit has been requested. "
                "Please contact support if it does not arrive."
            )
        else:
            self.send_customer_message("An error has occurred. Please contact support.")

    # --- Business Logic Methods ---
    def _refund_maintenance_deposit(self, amount, payment_method) -> None:
        """Send a coin that raced the payment-disable command straight back.

        Trust boundary: `payment_method` is a raw MQTT string, so a spoofed "test"
        can sit in escrow during a lease, but it cannot skip the sales table:
        recording is gated on `sale.is_test`, which only `run_test_sale` sets.
        """
        self._journal.deposit_during_maintenance(amount)
        self._escrow.deposit(payment_method, amount, time.time())
        self.last_payment_method = payment_method
        self.request_refund(reason="maintenance")

    @logger.catch()
    def deposit_funds(self, amount, payment_method="Simulated Payment"):
        logger.debug(f"Depositing funds: amount={amount:.2f}, method={payment_method}")
        if amount <= 0:
            self._journal.deposit_ignored(amount)
            return
        if self._in_maintenance() and payment_method != "test":
            self._refund_maintenance_deposit(amount, payment_method)
            return
        availability = self._availability()
        if availability and not availability.payment_enabled:
            self._journal.deposit_while_payment_disabled(
                amount, availability.payment_blocking_reasons()
            )
        self._escrow.deposit(payment_method, amount, time.time())
        self.last_payment_method = payment_method
        self._journal.deposited(amount, payment_method, self.credit_escrow)
        # Arm on every deposit, not only in interacting_with_user: credit can arrive
        # while idle (before any press, or while a soft fault refuses selections) and
        # would otherwise never be refunded (see _expire_session's idle branch).
        self._session_timer.arm()
        self._outputs.state_changed(self.state)
        self._outputs.refresh()
        self.send_customer_message(
            f"${amount:.2f} deposited. Current balance: ${self.credit_escrow:.2f}."
        )

    @logger.catch()
    def request_refund(self, reason: str = "admin"):
        """Pay the customer back: publish a refund command and await its ack.

        This is the ONLY path that sends money out. Restoring a price to
        escrow after a failed vend is not a refund and does not come here.
        """
        logger.debug(f"Requesting refund with current credit: {self.credit_escrow:.2f}")
        if self.credit_escrow <= 0:
            self.send_customer_message("No funds to refund.")
            return
        amount = self._escrow.take_all()
        pending = self._refunds.begin(amount, reason)
        self._journal.refund_requested(
            amount, self.last_payment_method, reason, pending.request_id
        )
        self._outputs.refresh()

    async def on_refund_ack(self, topic: str, data: dict):
        """Payment gateway acknowledged (or refused) a refund command."""
        result = PaymentRefundResult.model_validate(data)
        self._refunds.handle_ack(result)

    def _refund_confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._outputs.persist()
        self._journal.refund_confirmed(
            pending.request_id,
            pending.reason,
            amount_returned,
            self.last_payment_method,
        )

    def _refund_failed(self, pending: PendingRefund, detail: str) -> None:
        self._outputs.persist()
        self._journal.refund_failed(
            pending.request_id, pending.reason, pending.amount, detail
        )
        self.raise_fault(FaultCode.PAY_103, outcome=detail)

    # --- Maintenance lease support ---

    def make_idle_for_service(self) -> bool:
        """Make the machine idle for `MaintenanceLease.begin_standby`.

        Returns False, touching nothing, while `dispensing` (a running motor is
        never aborted). Otherwise refunds any credit (reason "maintenance"),
        cancels a live sale or the idle session timer, and returns True.
        """
        if self.state == "dispensing":
            return False
        if self.state == "interacting_with_user":
            # Refund first, so on_cancel_sale's own refund finds nothing:
            # exactly one refund, reason="maintenance".
            self.request_refund(reason="maintenance")
            self.cancel_sale()
        elif self.state == "idle":
            if not self._escrow.is_empty_within_tolerance:
                self.request_refund(reason="maintenance")
            self._session_timer.cancel()
        elif self.state == "error":
            if not self._escrow.is_empty_within_tolerance:
                self.request_refund(reason="maintenance")
        return True

    def find_product(self, sku: str) -> tuple[int | None, Product | None]:
        """Returns `(button_index, product)` for `sku` in the live catalog, or
        `(None, None)`; enumerating prevents collisions between value-equal products."""
        for index, product in enumerate(self.products):
            if product.sku == sku:
                return index, product
        return None, None

    def begin_test_sale(self, product: Product) -> bool:
        """Seed and select a simulated sale for `product`; True once it is selected.

        A second overlapping call raises (no await between check and set, so it is
        atomic). `self._sale` is seeded with `is_test=True` before `select_product`
        so its availability check sees the test sale. Callers pair this with
        `end_test_sale()` in a `finally`. If anything raises after the flag is set,
        the flag, seeded sale and "test" credit are cleared here (never via
        `request_refund`): a leaked flag would mark a later production sale's
        snapshot `is_test` and hide it from PAY-104 recovery.
        """
        if self._test_sale_in_progress:
            raise RuntimeError(
                "run_test_sale: a simulated sale is already in progress; "
                "wait for it to finish (or time out) before starting "
                "another"
            )
        self._test_sale_in_progress = True
        try:
            self._sale = SaleContext(
                product=product, is_test=True, started_at=time.time()
            )
            self.deposit_funds(round(product.price, 2), payment_method="test")
            index, _ = self.find_product(product.sku)
            self.select_product(index)
        except BaseException:
            self._escrow.take_all()
            self._test_sale_in_progress = False
            self._sale = None
            raise
        return (
            self.selected_product is product and self.state == "interacting_with_user"
        )

    def end_test_sale(self) -> None:
        """Release `begin_test_sale`'s guard and clean up after one simulated sale.

        While `dispensing` the context is left so the hardware report still settles
        it as a test sale (`_fail_vend` clears the credit then). Escrow is cleared
        directly, never via `request_refund`: test money is not real money.
        """
        if self.state != "dispensing":
            self._sale = None
        self._escrow.take_all()
        self._test_sale_in_progress = False

    @logger.catch()
    def initiate_virtual_payment(self, amount):
        """Show the next virtual-payment gateway's QR code to the customer."""
        prompt = self.payment_gateway_manager.next_payment_prompt(amount)
        if prompt is None:
            self.send_customer_message("Virtual payment is currently unavailable.")
            return

        current_gateway, qr_image = prompt
        self._outputs.show_qr(qr_image)
        self.send_customer_message(
            f"Virtual Payment Option ({current_gateway}): Scan the QR code above."
        )

    def _refusal(self, candidate: Product) -> bool:
        """Journal and message the refusal if `candidate` is locked out or unavailable; True when refused."""
        locked_code = self._faults.lockouts.get(candidate.sku)
        if locked_code is not None:
            self._journal.locked_out(candidate, locked_code)
            return True
        availability = self._availability()
        if availability:
            # A test sale (sale.is_test, never the lease) is exempt from its
            # own SVC-102 only; a customer press mid-lease is still refused.
            if self._sale is not None and self._sale.is_test:
                sellable, failing = availability.test_sale_sellable(candidate)
            else:
                sellable, failing = availability.product_sellable(candidate)
            if not sellable:
                self._journal.unavailable(candidate, failing[0])
                return True
        return False

    @logger.catch()
    def select_product(self, product_index):
        logger.debug(f"Selecting product with index: {product_index}")
        if self.state not in ["idle", "interacting_with_user"]:
            self._journal.selection_refused_not_ready()
            return
        if not (0 <= product_index < len(self.products)):
            self._journal.selection_refused_bad_index(product_index)
            return

        # `product_index` here is the physical button index (ButtonPress.button),
        # not the product's dispense `slot` — buttons stay positional for now.
        candidate = self.products[product_index]
        if self._refusal(candidate):
            return

        self.selected_product = candidate
        self._journal.product_selected(self.selected_product, product_index)

        inventory = self._inventory()
        if inventory and not inventory.is_available(self.selected_product.sku):
            self._journal.sold_out(self.selected_product)
            return

        if self.state == "idle":
            self.start_interaction()
            self._schedule(1.0, self.process_payment, label="process_payment")
        elif self.state == "interacting_with_user":
            self.initiate_virtual_payment(self.selected_product.price)
            self._schedule(1.0, self.process_payment, label="process_payment")
        self._outputs.refresh()

    @logger.catch()
    def process_payment(self):
        logger.debug(f"Processing payment for product: {self.selected_product}")
        if self.state != "interacting_with_user":
            logger.debug(
                "State is not interacting_with_user; aborting payment process."
            )
            return

        if self.selected_product is None or self.selected_product not in self.products:
            self._journal.catalog_lost_selection()
            self.cancel_sale()
            return

        price = self.selected_product.price if self.selected_product else 0
        if self.credit_escrow >= price:
            self._journal.payment_sufficient(
                self.credit_escrow, price, self.selected_product
            )
            self._sale = self._sale.with_(shares=self._escrow.consume_fifo(price))
            self.credit_escrow -= price
            logger.debug(
                f"Deducted price from escrow. New escrow: {self.credit_escrow:.2f} "
                f"(shares: {self.pending_sale_shares})"
            )
            self.dispense_product()
            self._outputs.persist("dispensing")
            self._outputs.refresh()
            self.last_insufficient_message = ""
        else:
            required = price - self.credit_escrow
            message = (
                f"Insufficient funds. Please insert an additional ${required:.2f}."
            )
            if message != self.last_insufficient_message:
                self._journal.payment_insufficient(
                    self.credit_escrow, price, self.selected_product, required, message
                )
                self.last_insufficient_message = message
            self._schedule(5.0, self.process_payment, label="process_payment")

    @logger.catch()
    def _expire_session(self):
        """Session-timeout timer fired: refund stranded credit and reset.

        Acts in `interacting_with_user` always (the customer walked away; also
        clears a lingering selection), and in `idle` or `error` only when
        escrow is positive (a deposit arrived after nothing could refund it).
        `idle` and `error` stay put; `interacting_with_user` is forced to
        `idle`. `dispensing` is a no-op: a vend in flight is never refunded
        out from under the customer, and it resolves before this could re-fire.
        """
        state = self.state
        if state == "dispensing":
            return
        if state != "interacting_with_user" and self.credit_escrow <= 0:
            return
        self._journal.session_timeout(self.credit_escrow, state)
        self.request_refund(reason="session_timeout")
        self._drop_sale()
        if state == "interacting_with_user":
            # Manually transition back to idle (reset_state only works from error)
            self.set_state("idle")
            self._outputs.state_changed(self.state)
            self._outputs.display("idle")
        else:
            self._outputs.state_changed(self.state)
        self._outputs.refresh()

    @logger.catch()
    def _finish_dispensing(self):
        logger.debug(
            f"Finishing dispensing process for product: {self.selected_product}"
        )
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        if self.state != "dispensing":
            logger.debug("State is not dispensing; cannot finish dispensing.")
            return
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        self._journal.finished_dispensing(product_name)
        self.send_customer_message("Product dispensed. Enjoy your purchase!")
        inventory = self._inventory()
        if inventory and self.selected_product:
            sku = self.selected_product.sku
            if inventory.is_tracked(sku):
                inventory.decrement(sku, persist=False)
                self._tasks.fire_and_forget(inventory.save_async(), persistent=True)
                self._journal.inventory_updated(
                    self.selected_product.name, inventory.get_count(sku)
                )
        self.complete_transaction()
        self._outputs.persist()
        self._outputs.refresh()
