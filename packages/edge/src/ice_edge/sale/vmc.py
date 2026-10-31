# ice_edge/sale/vmc.py
import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict
from loguru import logger
from ice_edge.payment.payment_gateway_manager import PaymentGatewayManager
from ice_contracts.board.mqtt_messages import ButtonPress
from ice_contracts.board.vending_machine import DispenserOutcome, FaultCode
from ice_common.config_model import ConfigModel, Product
from ice_contracts.money import format_cents
from ice_edge.faults.availability import Availability
from ice_edge.monitoring.inventory_manager import InventoryManager
from ice_edge.ledger.session_store import SessionSnapshot
from ice_edge.vend.dispense_cycle import DispenseCycle, DispenseReport
from ice_edge.faults.fault_service import FaultService
from ice_edge.ledger.escrow_ledger import EscrowLedger
from ice_edge.sale.buttons import product_for_button
from ice_edge.sale.outputs import StatusOutputs
from ice_edge.payment.ports import Payments, PendingPayout
from ice_edge.sale.payout_desk import PayoutDesk
from ice_edge.sale.sale_context import SaleContext
from ice_edge.sale.refusal import refusal
from ice_edge.sale.stock import draw_stock
from ice_edge.sale.session_timer import PaymentRetryTimer, SessionTimer
from ice_edge.supervisor.task_runner import TaskRunner
from ice_edge.vend.dispenser_gate import DispenserProfileGate
from ice_edge.sale.sale_journal import STATE_CHANGE_PREFIX, SaleJournal  # noqa: F401  (tests import STATE_CHANGE_PREFIX from here)


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

    @logger.catch()
    def __init__(
        self,
        config: ConfigModel,
        *,
        tasks: TaskRunner,
        escrow: EscrowLedger,
        payments: Payments,
        faults: FaultService,
        outputs: StatusOutputs,
        gate: DispenserProfileGate,
        in_maintenance: Callable[[], bool],
        availability: Callable[[], Availability | None],
        inventory: Callable[[], InventoryManager | None],
        dispense_factory: Callable[[SaleContext], DispenseCycle],
        journal: SaleJournal,
    ):
        """`Machine` (ice_edge/app/machine.py) is the sole constructor and passes every
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
        # credit_escrow_cents must always equal the sum of escrow_credits.
        self._escrow = escrow
        # One DispenseCycle per entry into 'dispensing'; None when none is in flight.
        self._dispense_factory = dispense_factory
        self._cycle: DispenseCycle | None = None
        self.last_insufficient_message = ""
        self.last_payment_method = "cash_coin"

        self._tasks = tasks
        self._session_timer = SessionTimer(
            tasks, lambda: self.session_timeout_seconds, self._expire_session
        )
        self._payment_retry = PaymentRetryTimer(tasks, self.process_payment)
        self._inventory: Callable[[], InventoryManager | None] = inventory
        self._availability: Callable[[], Availability | None] = availability
        self._outputs = outputs
        self._in_maintenance: Callable[[], bool] = in_maintenance
        # True for exactly one run_test_sale call (double-submit guard).
        self._test_sale_in_progress: bool = False
        self._state_change_observers = [self._payment_retry.on_state]
        self._sale_settled_observers: list[
            Callable[[SaleContext, str, str | None], None]
        ] = []
        self._faults = faults
        self._gate = gate
        self._payments = payments
        self._desk = PayoutDesk(escrow, payments, journal, self.raise_fault)
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
        self._payment_retry.cancel()

    @property
    def outputs(self) -> StatusOutputs:
        """Read-only view of the FSM's outbound channel."""
        return self._outputs

    def snapshot(self, state: str | None = None) -> SessionSnapshot:
        """Build a `SessionSnapshot` of the current live state (pure, no side effects)."""
        return self._snapshot(state)

    def _snapshot(self, state: str | None = None) -> SessionSnapshot:
        product = self.selected_product
        effective_state = state or self.state
        return SessionSnapshot(
            state=effective_state,
            credit_escrow_cents=self.credit_escrow_cents,
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
            pending_payouts=[asdict(p) for p in self._payments.pending_payouts()],
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
    # Properties over `self._escrow`; the setters replace `total_cents` and `credits`
    # independently, so the divergence guard in `EscrowLedger.consume_fifo` still works.

    @property
    def credit_escrow_cents(self) -> int:
        return self._escrow.total_cents

    @credit_escrow_cents.setter
    def credit_escrow_cents(self, value: int) -> None:
        self._escrow.total_cents = value

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

    async def on_button_press(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        self._journal.button_press(press.button)
        product = product_for_button(self.products, press.button)
        if product is None:
            self._journal.selection_refused_unknown_button(press.button)
            return
        self.select_product(product)

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
        # sale that already settled through the hardware report (`cycle.settled`
        # covers the window while its sale write is still awaited).
        if cycle is not self._cycle:
            self._journal.late_failure_ignored(code, outcome)
            return
        if self.state != "dispensing" or cycle.settled:
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
        # Settle (cancels the timeout) before the await in record(): the customer already has
        # the product, so a timeout firing during that suspension must not fail it.
        self._cycle.settle()
        self._journal.dispense_success(slot, product_name)
        sale, cycle = self._sale, self._cycle
        if sale is not None and sale.is_test:
            # is_test lives on the sale, not the lease, so a lease release cannot flip it
            # to production; record() is skipped, so no sale/dispense rows are written.
            self._sale = sale.with_(shares=None)
        else:
            recorded = await cycle.record()
            if recorded and sale is not None:
                self._desk.cash_sale(sale)
            # `False` is the PAY-104 path: shares stay as the sale's only record.
            # `cycle is self._cycle` = still this sale (reset/a new customer replace it).
            if recorded and cycle is self._cycle and self._sale is not None:
                self._sale = self._sale.with_(shares=None)
        # Raised after the sale is recorded and before _finish_dispensing, on purpose.
        if report.outcome is DispenserOutcome.door_open:
            # Customer success that is also a hardware fault (trap door did not close).
            sku = sale.product.sku if sale is not None else None
            self.raise_fault(FaultCode.ICE_402, sku=sku, outcome=report.outcome.value)
        if cycle is not self._cycle or self.state != "dispensing":
            # on_error/reset (or a later sale) took over while the row was written: the
            # product is out, so settle without the illegal dispensing -> idle transition.
            self._journal.sale_write_finished_in_state(self.state)
            if sale is not None:
                draw_stock(self._inventory(), self._tasks, self._journal, sale.product)
                self._outputs.persist()
                self._notify_settled(sale, "dispensed", None)
            return
        self._settle("dispensed", None, self._finish_dispensing)

    def _dispense_failed_report(self, report: DispenseReport, slot: object) -> None:
        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        self._cycle.settle()
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
        self._payment_retry.cancel()
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
            "credit_escrow_cents": self.credit_escrow_cents,
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
        # Price was already deducted; leftover credit is paid out as change at once.
        self._transition("dispensing", "idle", self.on_complete_transaction)

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
        self._journal.interaction_started()

    @logger.catch()
    def on_dispense_product(self):
        self._journal.entering_dispensing(self.selected_product)
        self._session_timer.cancel()
        self._outputs.display("dispensing")
        self._outputs.refresh()
        self._journal.dispensing_started()
        # Dispatch uses the product's own `slot`, not its list index: deleting an
        # earlier product shifts indices but not the physical slot.
        if self._tasks.loop and self.selected_product:
            self._cycle = self._dispense_factory(self._sale)
            self._cycle.start(
                lambda s: self._snapshot(s) if self._outputs.session_store else None
            )

    @logger.catch()
    def on_complete_transaction(self):
        self._journal.completing(self.credit_escrow_cents)
        had_change = self.credit_escrow_cents > 0
        sale_id = self._sale.sale_id if self._sale is not None else None
        # Clear the selection: a stale selection would let a late duplicate dispenser fault
        # refund at the old product's price.
        self.selected_product = None
        self._journal.transaction_complete()
        if had_change:
            self.request_refund(reason="change", sale_id=sale_id)
        self._outputs.display("idle")
        self._outputs.refresh()

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
        self._journal.product_removed()

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
        pieces = self._sale.pieces if self._sale is not None else None
        if self._sale is not None:
            self._sale = self._sale.with_(shares=None, pieces=None)
        if shares is None:
            # Unreachable guard: consume_fifo sets the shares before dispensing.
            price = product.price_cents if product else 0
            self._journal.shares_missing(price)
            shares = {"unknown": price}
        else:
            # Price from the shares, not product.price_cents: the live catalog object's
            # price can be edited mid-dispense.
            price = sum(shares.values())
        now = time.time()
        self._escrow.restore(shares, now, price, pieces)
        is_test = self._sale is not None and self._sale.is_test
        self.selected_product = None
        self.last_insufficient_message = ""
        self._journal.vend_failed(name, code, outcome, price, sku, is_test=is_test)

    def _fail_vend(self, code: FaultCode, outcome: str) -> None:
        """Run the vend_failed transition, then decide: choose again, or pay out."""
        # Captured BEFORE vend_failed: on_vend_failed clears self._sale.
        is_test = self._sale is not None and self._sale.is_test
        sale_id = self._sale.sale_id if self._sale is not None else None
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
                self.request_refund(
                    reason="vend_failed", detail=code.value, sale_id=sale_id
                )
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
        sale, cycle = self._sale, self._cycle
        # A settled cycle means the product is out and its row is being written; it stays
        # on the VMC (timer cancelled) so a re-entry takes this branch again and the
        # finishing write can tell it is still the same sale.
        written = cycle is not None and cycle.settled
        if not written:
            self._cycle = None
        if cycle is not None:
            cycle.cancel()
        if written:
            self._journal.error_during_sale_write(self.selected_product)
        elif shares := self.pending_sale_shares:
            restored = self._escrow.restore_shares(
                shares, sale.pieces if sale else None
            )
            self._journal.error_restored(restored)
        if sale is not None:
            shares_kept = sale.shares if written else None
            sale = self._sale = sale.with_(
                mechanism=None, request_id=None, shares=shares_kept
            )
            if sale.is_test:  # never refund test money
                self._journal.test_credit_cleared_on_error(self._escrow.take_all())
        if had_credit := self.credit_escrow_cents > 0:
            self.request_refund(reason="error")
        self._outputs.display("error")
        self._outputs.refresh()
        self._journal.error_message(had_credit)
        if sale is not None and not written and not sale.settled:
            self._notify_settled(sale, "error", None)
            self._sale = sale.with_(settled=True)

    # --- Business Logic Methods ---
    def _refund_maintenance_deposit(
        self, cents: int, payment_method, event_id: str | None
    ) -> None:
        """Send a coin that raced the payment-disable command straight back.

        Trust boundary: `payment_method` is a raw MQTT string, so a spoofed "test"
        can sit in escrow during a lease, but it cannot skip the sales table:
        recording is gated on `sale.is_test`, which only `run_test_sale` sets.
        """
        self._journal.deposit_during_maintenance(cents)
        self._escrow.deposit(payment_method, cents, time.time(), event_id)
        self.last_payment_method = payment_method
        self.request_refund(reason="maintenance")

    def deposit_funds(self, cents: int, payment_method="cash_coin", *, event_id=None):
        """Credit escrow. Not wrapped in `logger.catch`: the MDB intake acks a
        payment event only if this returns, so a failure must propagate (the
        board then replays the event). Everything after the credit itself is
        best-effort (`_after_deposit`) and can never undo or repeat it."""
        logger.debug(f"Depositing funds: cents={cents}, method={payment_method}")
        if cents <= 0:
            self._journal.deposit_ignored(cents)
            return
        if self._in_maintenance() and payment_method != "test":
            self._refund_maintenance_deposit(cents, payment_method, event_id)
            return
        self._escrow.deposit(payment_method, cents, time.time(), event_id)
        self.last_payment_method = payment_method
        self._after_deposit(cents, payment_method)

    @logger.catch()
    def _after_deposit(self, cents: int, payment_method) -> None:
        availability = self._availability()
        if availability and not availability.payment_enabled:
            self._journal.deposit_while_payment_disabled(
                cents, availability.payment_blocking_reasons()
            )
        self._journal.deposited(cents, payment_method, self.credit_escrow_cents)
        # Arm on every deposit, not only in interacting_with_user: credit can arrive
        # while idle (before any press, or while a soft fault refuses selections) and
        # would otherwise never be refunded (see _expire_session's idle branch).
        self._session_timer.arm()
        self._outputs.state_changed(self.state)
        self._outputs.refresh()
        self.send_customer_message(
            f"{format_cents(cents)} deposited. "
            f"Current balance: {format_cents(self.credit_escrow_cents)}."
        )

    @logger.catch()
    def request_refund(
        self,
        reason: str = "admin",
        *,
        detail: str | None = None,
        sale_id: str | None = None,
    ):
        """Pay the customer back: queue a cash payout on the MDB board.

        The ONLY path that sends money out (see `PayoutDesk.refund`);
        restoring a price to escrow after a failed vend is not a refund.
        """
        logger.debug(f"Requesting refund, credit cents: {self.credit_escrow_cents}")
        if self.credit_escrow_cents <= 0:
            self.send_customer_message("No funds to refund.")
            return
        self._desk.refund(reason, detail, sale_id, self.last_payment_method)
        self._outputs.refresh()

    def on_payment_lever(self) -> None:
        """The customer pressed the coin-return lever: refund, unless a vend is running."""
        if self.state == "dispensing" or self._escrow.is_empty:
            logger.info(f"Coin-return lever ignored (state={self.state})")
            return
        self.request_refund(reason="customer")

    def on_payout_paid(self, item: PendingPayout, paid_cents: int) -> None:
        """The board confirmed (all or part of) a payout; the queue persists itself."""
        self._journal.refund_confirmed(
            item.request_id, item.reason, paid_cents, self.last_payment_method
        )

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
            if not self._escrow.is_empty:
                self.request_refund(reason="maintenance")
            self._session_timer.cancel()
        elif self.state == "error":
            if not self._escrow.is_empty:
                self.request_refund(reason="maintenance")
        return True

    def find_product(self, sku: str) -> Product | None:
        """The product with `sku` in the live catalog, or `None`."""
        return next((p for p in self.products if p.sku == sku), None)

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
            self.deposit_funds(product.price_cents, payment_method="test")
            self.select_product(product)
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
    def initiate_virtual_payment(self, amount_cents: int):
        """Show the next virtual-payment gateway's QR code to the customer."""
        prompt = self.payment_gateway_manager.next_payment_prompt(amount_cents)
        if prompt is None:
            self.send_customer_message("Virtual payment is currently unavailable.")
            return

        current_gateway, qr_image = prompt
        self._outputs.show_qr(qr_image)
        self.send_customer_message(
            f"Virtual Payment Option ({current_gateway}): Scan the QR code above."
        )

    def _refusal(self, candidate: Product, *, stock: bool = False) -> str | None:
        return refusal(
            candidate,
            faults=self._faults,
            availability=self._availability(),
            inventory=self._inventory(),
            journal=self._journal,
            is_test=self._sale is not None and self._sale.is_test,
            check_stock=stock,
        )

    @logger.catch()
    def select_product(self, product: Product) -> None:
        logger.debug(f"Selecting product: {product.sku}")
        if self.state not in ["idle", "interacting_with_user"]:
            self._journal.selection_refused_not_ready()
            return

        candidate = product
        if self._refusal(candidate, stock=True):
            return

        self.selected_product = candidate
        self._journal.product_selected(self.selected_product)

        if self.state == "idle":
            self.start_interaction()
            self._payment_retry.arm()
        elif self.state == "interacting_with_user":
            self.initiate_virtual_payment(self.selected_product.price_cents)
            self._payment_retry.arm()
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

        # Availability or stock can change while the customer is still paying.
        if code := self._refusal(self.selected_product, stock=True):
            if self._sale.is_test:  # settle it, or the runner awaits forever
                self._escrow.take_all()  # test money is never refunded
                self._settle("vend_failed", code, self.cancel_sale)
            else:
                self.selected_product = None
                self._outputs.refresh()
            return

        price = self.selected_product.price_cents
        escrow = self.credit_escrow_cents
        if escrow >= price:
            self._journal.payment_sufficient(escrow, price, self.selected_product)
            shares, pieces = self._escrow.consume_fifo_pieces(price)
            self._sale = self._sale.with_(shares=shares, pieces=tuple(pieces))
            self.credit_escrow_cents -= price
            logger.debug(
                f"Deducted price from escrow. New escrow cents: {self.credit_escrow_cents} "
                f"(shares: {self.pending_sale_shares})"
            )
            self.dispense_product()
            self._outputs.persist("dispensing")
            self._outputs.refresh()
            self.last_insufficient_message = ""
        else:
            required = price - escrow
            message = f"Insufficient funds. Please insert an additional {format_cents(required)}."
            if message != self.last_insufficient_message:
                self._journal.payment_insufficient(
                    escrow, price, self.selected_product, required, message
                )
                self.last_insufficient_message = message
            self._payment_retry.arm(5.0)

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
        if state != "interacting_with_user" and self.credit_escrow_cents <= 0:
            return
        self._journal.session_timeout(self.credit_escrow_cents, state)
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
        draw_stock(self._inventory(), self._tasks, self._journal, self.selected_product)
        self.complete_transaction()
        self._outputs.persist()
        self._outputs.refresh()
