# controller/vmc.py
import asyncio
import time
from collections.abc import Callable
from transitions import Machine
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
from controller.dispense_cycle import DispenseCycle
from controller.fault_service import FaultService
from controller.escrow_ledger import EscrowLedger
from controller.outputs import StatusOutputs
from controller.refund_protocol import PendingRefund, RefundProtocol
from controller.sale_context import SaleContext
from controller.task_runner import TaskRunner
from controller.dispenser_gate import DispenserProfileGate

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"


# Bound loggers — initialized lazily so sinks are installed before first use.
# Module-level references are set by VMC.__init__() (after setup_logging() in main.py).
# ice_log lives in controller/mqtt_inbound.py now, bound at import.
txn_log = logger
vend_log = logger

#: FSM transition table.
#: Ordering matters when multiple transitions share the same trigger name.
TRANSITIONS = [
    {
        "trigger": "start_interaction",
        "source": "idle",
        "dest": "interacting_with_user",
        "before": "on_start_interaction",
    },
    {
        "trigger": "dispense_product",
        "source": "interacting_with_user",
        "dest": "dispensing",
        "before": "on_dispense_product",
    },
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "interacting_with_user",
        "conditions": "has_credit",
        "before": "on_complete_transaction",
    },
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "idle",
        "unless": "has_credit",
        "before": "on_complete_transaction",
    },
    {
        "trigger": "cancel_sale",
        "source": "interacting_with_user",
        "dest": "idle",
        "before": "on_cancel_sale",
    },
    {
        "trigger": "vend_failed",
        "source": "dispensing",
        "dest": "interacting_with_user",
        "before": "on_vend_failed",
    },
    {
        "trigger": "error_occurred",
        "source": "*",
        "dest": "error",
        "before": "on_error",
    },
    {
        "trigger": "reset_state",
        "source": ["error"],
        "dest": "idle",
        "before": "on_reset",
    },
]


class VMC:
    """The vending machine's sale finite-state machine (states: `idle`,
    `interacting_with_user`, `dispensing`, `error`; see `TRANSITIONS`
    above).

    `VMC` holds no transport or service handle -- no MQTT client, health
    monitor, session store, event recorder, command dispatcher, display
    controller, or maintenance-lease reference. Every collaborator it
    needs is injected by `controller/machine.py`'s `Machine`, the sole
    construction path: `tasks` (`TaskRunner`), `escrow` (`EscrowLedger`),
    `refunds` (`RefundProtocol`), `faults` (`FaultService`), `outputs`
    (`StatusOutputs`), `gate` (`DispenserProfileGate`), a `DispenseCycle`
    factory, and three read-only callables (`in_maintenance`,
    `availability`, `inventory`, `recorder`). Everything the FSM
    publishes, persists, displays, or messages to the customer goes
    through `outputs` (`StatusOutputs`, its only outbound channel); every
    fault it raises or clears goes through `faults` (`FaultService`,
    wrapping `FaultRegistry`). One `DispenseCycle` is built per entry into
    `dispensing` and owns that sale's dispatch/timeout/classify/record
    conversation with the board, holding the VMC's own identity-based
    guard against a late callback from a superseded attempt.
    `subscribe_state_change`/`subscribe_sale_settled` let other
    collaborators (chiefly `controller/test_sale.py`'s `TestSaleRunner`)
    observe a sale settling without the FSM knowing they exist.
    """

    states = ["idle", "interacting_with_user", "dispensing", "error"]

    REFUND_ACK_TIMEOUT = 10.0  # seconds to wait for cmd/payment/refund/ack
    REFUND_MAX_ATTEMPTS = 2  # one retry with the same request_id, then PAY-103

    # Amounts within this many dollars of each other are the same money for
    # ledger purposes. Every amount in this system is meaningful only to the
    # cent (round(x, 2) is used throughout, e.g. request_refund below), so a
    # residue smaller than half a cent can only be float noise — 0.1 + 0.2
    # deposited as two credits and then spent as one 0.3 sale leaves a
    # remainder around 4e-17, many orders of magnitude under this — and can
    # never be a real, distinguishable amount of money. Half a cent is also
    # the largest tolerance that can never itself be mistaken for a whole
    # cent: an actual one-cent credit ($0.01) is always kept.
    # Equal to EscrowLedger.TOLERANCE -- kept as a VMC class attribute
    # because callers (and tests) reference it as VMC.CREDIT_TOLERANCE.
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
        recorder: Callable[[], object | None],
        dispense_factory: Callable[[SaleContext], DispenseCycle],
    ):
        """`controller/machine.py`'s `Machine` is the only construction
        path -- every collaborator parameter above is required. `Machine`
        builds `tasks`/`escrow`/`refunds`/`faults`/`outputs`/`gate` itself
        and hands them in fully formed; every closure inside each one
        that needs the live VMC (e.g. `outputs.snapshot`,
        `faults.fsm_state`) is written by `Machine` as `lambda *a:
        self.vmc.<method>(*a)`, so there is no circularity even though
        `Machine` builds them before its own `self.vmc` exists.

        `in_maintenance` is a zero-arg callable (`Machine` passes
        `lambda: self.lease.hold is not None`) -- `deposit_funds` is its
        only reader. The lease itself (its hold, idle timer, standby
        sweep, and every FSM precondition it needs --
        `begin_maintenance`/`begin_standby`/`end_maintenance`/
        `take_over_maintenance`) lives entirely on the `MaintenanceLease`
        collaborator, read and mutated by tests and routes as
        `machine.lease`; the VMC never holds a reference to it at all,
        only this one boolean read.

        `availability`/`inventory`/`recorder` are zero-arg callables
        (`Machine` passes `lambda: self.availability`/
        `lambda: self.inventory`/`lambda: self.event_recorder`) rather than
        the objects themselves, because the live instance is attached well
        after construction, via `Machine.set_availability`/
        `set_inventory_manager`/`set_event_recorder`; every internal read
        goes through `self._availability()`/`self._inventory()`/
        `self._recorder()` so it always sees whatever is current.

        `dispense_factory` builds a fresh `DispenseCycle`
        (controller/dispense_cycle.py) for one sale's dispatch attempt --
        `Machine` builds it as a closure over itself carrying the
        dispatcher, gate, outputs, faults, recorder, and
        `set_transaction_certain` collaborators that class needs, plus
        `on_failed=self.vmc.on_dispense_failed`/
        `on_request_id=self.vmc.note_dispense_request`. The cycle's own
        *identity* (one instance per dispatch attempt), not a sequence
        number, is what `on_dispense_failed` uses to tell a late callback
        from an earlier attempt apart from the current one.
        """
        global txn_log, vend_log
        txn_log = logger.bind(transaction=True)
        vend_log = logger.bind(vending=True)
        logger.debug("Initializing VMC with pre-loaded ConfigModel")

        self.config_model = config
        logger.debug(self.config_model.model_dump_json(exclude_none=True, indent=2))

        self.products = self.config_model.products
        self.owner_contact = self.config_model.machine_owner

        # The in-flight sale (controller/sale_context.py's SaleContext):
        # product, consumed escrow shares, test-ness, dispenser mechanism,
        # and dispatch request_id, all replaced wholesale at
        # each transition rather than written as separate attributes. None
        # whenever no sale is in progress. Exposed read-only as `self.sale`
        # (VMC public surface design, section 3); `selected_product` and
        # `pending_sale_shares` below stay read/write and read-only
        # properties, respectively, over this -- see SaleContext's own
        # module docstring for the full field-by-field rationale.
        self._sale: SaleContext | None = None
        # credit_escrow/escrow_credits live in self._escrow (an
        # EscrowLedger, controller/escrow_ledger.py) -- the FIFO ledger
        # behind the authoritative total. credit_escrow must always equal
        # round(sum(c.amount for c in escrow_credits), 2) — the two are
        # never allowed to diverge (see EscrowLedger.consume_fifo's bug guard).
        # VMC.credit_escrow/escrow_credits below are the public read/write
        # surface over self._escrow.total/self._escrow.credits.
        self._escrow = escrow
        # The current in-flight dispense's DispenseCycle
        # (controller/dispense_cycle.py), built fresh by `dispense_factory`
        # once per on_dispense_product call (one dispatch attempt per entry
        # into 'dispensing'). None whenever no dispatch is in flight. The
        # cycle's own identity -- not a sequence number -- is what
        # on_dispense_failed compares against to tell a late callback from
        # an earlier attempt apart from the current one.
        self._dispense_factory: Callable[[SaleContext], DispenseCycle] = (
            dispense_factory
        )
        self._cycle: DispenseCycle | None = None
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"

        # Event-loop task plumbing (controller/task_runner.py): fire-and-
        # forget tasks, delayed callbacks, and the persistent-task set
        # drained (not cancelled) at shutdown. Exposed read-only as
        # `self.tasks` (VMC public surface design, section 3); tests read
        # `vmc.tasks.pending`/`vmc.tasks.persist`/`vmc.tasks.loop` directly
        # rather than a VMC-private alias. Constructed before anything that
        # captures `self._schedule` as a closure (the maintenance lease and
        # refund protocol below), since that closure's first real call must
        # find a live runner. `tasks=` (VMC public surface design, section
        # 2) lets a test inject `tests.fakes.FakeTaskRunner` instead of a
        # real event-loop runner, so timers can be fired by label rather
        # than through a private task handle; defaults to a real
        # `TaskRunner()` for every production and non-timer-test caller.
        self._tasks = tasks
        self._session_timeout_task: asyncio.Task | None = None
        # `self._inventory`/`self._availability`/`self._recorder` are
        # always the zero-arg callables `Machine` passed in (see the
        # __init__ docstring above) -- never a plain attribute assigned by
        # a VMC-side `set_*` method, which does not exist.
        self._inventory: Callable[[], InventoryManager | None] = inventory
        self._availability: Callable[[], Availability | None] = availability
        self._recorder: Callable[[], object | None] = recorder
        # Outbound side effects -- MQTT publishes, session persistence, the
        # customer display, and the dashboard's UI-refresh/message/QR
        # callbacks (controller/outputs.py's StatusOutputs) -- the FSM's
        # only outbound channel. Exposed read-only as `self.outputs` (VMC
        # public surface design, section 3); tests read
        # `vmc.outputs.mqtt`/`.health`/etc. directly rather than a
        # VMC-private alias.
        self._outputs = outputs
        # The maintenance lease itself (the
        # `MaintenanceLease` collaborator) now lives entirely off the VMC,
        # owned by `Machine` and read/mutated by tests and routes as
        # `machine.lease`. `deposit_funds` is the VMC's one remaining
        # lease-aware read, via this boolean callable -- see the __init__
        # docstring above.
        self._in_maintenance: Callable[[], bool] = in_maintenance
        # True for the duration of exactly one TestSaleRunner.run_test_sale
        # call. TestSaleRunner.run_test_sale checks this flag itself,
        # before `self._lease.test_run()` is ever entered, so a refused
        # double-submit never refreshes the lease's idle clock;
        # begin_test_sale then sets it (as defence in depth for a caller
        # that reaches it directly), and end_test_sale clears it in that
        # call's own outer `finally`. See begin_test_sale/
        # TestSaleRunner.run_test_sale's own comments for why run_id
        # uniqueness alone does not do this.
        self._test_sale_in_progress: bool = False
        # Observer hooks: every
        # FSM state transition and every sale-settled outcome is
        # broadcast to subscribers via subscribe_state_change/
        # subscribe_sale_settled, in subscription order, synchronously.
        # run_test_sale (this task) is the first consumer -- it
        # subscribes both for the duration of one simulated sale instead
        # of the VMC holding a waiter/path pair of its own -- but neither
        # list has any knowledge of run_test_sale or test sales at all.
        self._state_change_observers: list[Callable[[str], None]] = []
        self._sale_settled_observers: list[
            Callable[[SaleContext, str, str | None], None]
        ] = []
        # Fault service: product-scope faults by SKU, machine-scope faults
        # by code, wrapped by controller/fault_service.py's `FaultService`
        # (event-recorder rows, health/MQTT alerts, the availability/health
        # active-fault push, and the liveness/MQTT-connection fault
        # mappings). Built by `Machine`. Exposed read-only as `self.faults`
        # (VMC public surface design, section 3); tests read
        # `vmc.faults.lockouts`/`vmc.faults.is_locked(...)`/`vmc.faults.has(...)`
        # directly rather than a VMC-private alias, and mutate only through
        # `raise_fault`/`clear_fault`, never the registry directly.
        self._faults = faults
        # Dispenser-profile gate: CFG-101/CFG-102 reconciliation against a
        # loaded DispenserProfiles (controller/dispenser_gate.py). Built by
        # `Machine`, attached via `Machine.set_dispenser_profiles`; this
        # VMC still reads `self._gate` internally (`run_test_sale`'s
        # profile check, `on_dispense_product`, `clear_fault`'s closure)
        # but no longer exposes a public `gate` property --
        # `machine.gate` instead.
        self._gate = gate
        # Refund protocol: request -> ack -> one retry -> terminal state
        # machine (controller/refund_protocol.py). Built by `Machine`.
        # Exposed read-only as `self.refunds` (VMC public surface design,
        # section 3); tests read `vmc.refunds.pending` directly rather than
        # a VMC-private alias.
        self._refunds = refunds
        # Public (no leading underscore) so a test can read/override them
        # directly (VMC public surface design, section 2) rather than
        # reaching into a private attribute -- e.g.
        # `test_timeout_seconds_come_from_config` reads
        # `dispense_timeout_seconds`, and a handful of real-timer tests set
        # a short override before letting the real TaskRunner fire it.
        self.session_timeout_seconds = 180.0  # 3 minutes
        self.dispense_timeout_seconds = (
            self.config_model.physical.dispense_timeout_seconds
        )

        self.machine = Machine(
            model=self,
            states=VMC.states,
            initial=VMC.states[0],
            auto_transitions=False,
            after_state_change="_after_state_change",
        )

        for t in TRANSITIONS:
            self.machine.add_transition(**t)
        logger.debug("FSM transitions set up successfully.")

        self.payment_gateway_manager = PaymentGatewayManager(
            config=self.config_model.payment.model_dump()
        )

        logger.debug("VMC initialization complete.")

    def cancel_timers(self) -> None:
        """Cancel the dispense cycle's timer and the session timeout. Call
        during shutdown (`Machine.cancel_pending_tasks`) -- the
        remaining VMC-private pieces of what `cancel_pending_tasks` used to
        do before it moved to `Machine` entirely."""
        if self._cycle is not None:
            self._cycle.cancel()
        self._cancel_session_timeout()

    @property
    def outputs(self) -> StatusOutputs:
        """Read-only view of the FSM's outbound channel (VMC public
        surface design, section 3).
        `vmc.outputs.mqtt`/`.health`/`.availability`/`.session_store`/
        `.display_controller` are read directly; mutation only ever
        happens through a `Machine` `set_*` method."""
        return self._outputs

    def reconcile_session(self) -> None:
        """Future hook: query the payment gateway for held credit and clear
        PAY-104 automatically. The contract has no credit query yet, so the
        operator clears the fault from the dashboard after checking the machine.
        """
        return None

    def snapshot(self, state: str | None = None) -> SessionSnapshot:
        """Build a `SessionSnapshot` of the VMC's current live state.

        Public, read-only wrapper over `_snapshot` (VMC public surface
        design, section 3): added because a test needs to capture
        a genuine mid-dispense snapshot and persist it by hand (to boot a
        second VMC against it and simulate a crash) with no event loop
        attached anywhere in the test file -- the normal production path
        (`DispenseCycle._run`/`self._outputs.persist`) is fire-and-forget
        on the attached loop and cannot run there. Pure and side-effect free
        (matches `get_status()`'s existing read-only convenience), so
        exposing it costs nothing: it only ever reads already-public state
        (`state`, `credit_escrow`, `selected_product`) plus collaborators
        already public via `refunds`/`escrow`.
        """
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
            # self._sale is already None by the time a failed-vend's own
            # state_changed() snapshot is taken (on_vend_failed fully
            # clears it before _fail_vend gets to that point) -- fall back
            # to the re-entrancy guard, which strictly contains the whole
            # run_test_sale call, so a test sale's "no sellable products
            # remain" snapshot still reads is_test=True instead of
            # wrongly raising PAY-104 for test money on a crash right
            # there. A production sale never has this flag set, so the
            # fallback is False for it exactly as self._sale_is_test was.
            is_test=(
                self._sale.is_test
                if self._sale is not None
                else self._test_sale_in_progress
            ),
        )

    # --- Fault service ---
    #
    # State and side effects live in `self._faults`
    # (controller/fault_service.py's `FaultService`, wrapping
    # controller/fault_registry.py's `FaultRegistry` for the pure
    # bookkeeping); the methods below are one-line forwards.

    @property
    def faults(self) -> FaultService:
        """Read-only view of the fault service (VMC public surface
        design, section 3; see controller/fault_service.py). `vmc.faults.lockouts`/`.is_locked(sku)`/
        `.has(code)` are read directly; mutation only ever happens through
        `raise_fault`/`clear_fault`."""
        return self._faults

    # --- Escrow ledger ---
    #
    # State and pure bookkeeping live in `self._escrow`
    # (controller/escrow_ledger.py's `EscrowLedger`); the properties below
    # are the public read/write surface over it -- `credit_escrow`/
    # `escrow_credits`. The setters only ever replace `total`/`credits` on
    # the ledger -- a direct
    # `vmc.credit_escrow = x` assignment still cannot touch `credits`,
    # which is what lets the divergence guard in `EscrowLedger.consume_fifo`
    # keep working exactly as before this extraction.

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
        """Read-only view of the escrow ledger (VMC public surface
        design, section 3) -- `credit_escrow`/`escrow_credits` above stay
        the primary read/write surface; this is for callers that want the
        ledger object itself (e.g. `vmc.escrow.is_empty_within_tolerance`)."""
        return self._escrow

    @property
    def refunds(self) -> RefundProtocol:
        """Read-only view of the refund protocol (VMC public surface
        design, section 3). `vmc.refunds.pending` is read directly;
        mutation only ever happens through `request_refund`/`on_refund_ack`."""
        return self._refunds

    # --- In-flight sale (SaleContext, controller/sale_context.py) ---
    #
    # `self._sale` is the single source of truth; every FSM callback below
    # replaces it wholesale (`self._sale = self._sale.with_(...)` or
    # `self._sale = None`) rather than writing the old separate attributes
    # (`selected_product`, `pending_sale_shares`, `_sale_is_test`,
    # `_sale_mechanism`, `_dispense_request_id`) by hand. `selected_product`
    # stays read/write -- many existing tests assign
    # `vmc.selected_product = vmc.products[0]` directly to set up a sale
    # without going through `select_product` -- and `pending_sale_shares`
    # stays read-only, both as properties over `self._sale`.

    @property
    def sale(self) -> SaleContext | None:
        """Read-only view of the in-flight sale (VMC public surface
        design, section 3). `None` whenever no sale is in progress."""
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
        # else: the same product is already the in-flight sale's product --
        # leave the existing context (shares/mechanism/request_id/is_test)
        # untouched. This is what lets `run_test_sale` seed a context with
        # `is_test=True` before calling `select_product`, which then goes
        # on to assign that same product right back here.

    @property
    def pending_sale_shares(self) -> dict[str, float] | None:
        return self._sale.shares if self._sale is not None else None

    @property
    def test_sale_in_progress(self) -> bool:
        """Read-only view of the re-entrancy guard around
        `run_test_sale` -- see `self._test_sale_in_progress`'s own
        comment."""
        return self._test_sale_in_progress

    def _product_name(self, sku: str | None) -> str | None:
        if sku is None:
            return None
        return next((p.name for p in self.products if p.sku == sku), sku)

    def _sellable_products(self) -> list:
        return [p for p in self.products if self._faults.is_locked(p.sku) is None]

    def active_faults(self) -> list[dict]:
        """Snapshot for the dashboard/health monitor. Forwards to
        `self._faults.active_faults()` (controller/fault_service.py)."""
        return self._faults.active_faults()

    def raise_fault(
        self,
        code: FaultCode,
        *,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record a fault. Forwards to `self._faults.raise_fault(...)`
        (controller/fault_service.py) -- see that class's docstring for
        the full contract."""
        self._faults.raise_fault(code, sku=sku, outcome=outcome)

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key. Forwards to `self._faults.clear_fault(...)`
        (controller/fault_service.py) -- see that class's docstring for
        the full contract."""
        return self._faults.clear_fault(key, by=by)

    # --- MQTT inbound handlers ---

    async def on_payment_credit(self, topic: str, data: dict):
        """Handle payment credit from MDB ESP32."""
        event = PaymentEvent.model_validate(data)
        logger.info(f"MQTT payment received: ${event.amount:.2f} via {event.method}")
        txn_log.info(f"PAYMENT RECEIVED: ${event.amount:.2f} via {event.method}")
        self.deposit_funds(event.amount, payment_method=event.method)

    async def on_button_press(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        logger.info(f"MQTT button press: button {press.button}")
        txn_log.info(f"BUTTON PRESS: button {press.button}")
        vend_log.info(f"BUTTON PRESS: button {press.button}")
        self.select_product(press.button)

    def note_dispense_request(self, request_id: str, mechanism: str) -> None:
        """Record the dispatch `request_id`/`mechanism` a `DispenseCycle`
        just minted for the in-flight sale, before it dispatches -- the
        `on_request_id` callback the factory (controller/machine.py)
        supplies to every `DispenseCycle` it builds."""
        self._sale = self._sale.with_(mechanism=mechanism, request_id=request_id)

    async def on_dispense_failed(
        self, cycle: DispenseCycle, code: FaultCode, outcome: str
    ) -> None:
        """Fail an in-flight dispense, but only if `cycle` still names the
        *current* dispatch and the FSM is still in 'dispensing' when this
        actually runs. The `on_failed` callback every `DispenseCycle` is
        built with (controller/machine.py); called for a dispatch failure
        (`_run`'s own failure paths, including the CFG-101 no-profile
        fallback in `start`) and for the no-terminal-report timeout
        (`_timed_out`) alike.

        `cycle`'s own identity (replacing review finding I2's old `seq`
        counter) guards against a late failure from an
        *earlier* sale's dispatch reaching here after that sale has
        already settled and a new sale has since reached 'dispensing':
        without this check, a delayed `CommandTimeout` for sale A (the
        dispatcher's own timeout window, not the dispense-timeout
        fallback) could cancel sale B's dispense timer, raise PAY-102 on
        B's sku, and refund B's price while B's product is actually being
        dispensed. Only the cycle that is still current -- `cycle is
        self._cycle` -- may fail the vend.

        The state check below additionally covers the case where the
        *same* sale already settled through the real hardware report (a
        fire-and-forget dispatch task can race the terminal
        `hardware/dispenser` report), so a settled sale must never be
        double-failed.
        """
        if cycle is not self._cycle:
            logger.warning(
                f"late dispatch failure for a cycle that is no longer current "
                f"ignored (code={code.value}, outcome={outcome!r})"
            )
            return
        if self.state != "dispensing":
            logger.debug(
                "Dispense failed after the sale already left 'dispensing'; "
                "ignoring (not double-failing a settled sale)."
            )
            return
        cycle.cancel()
        sku = self.selected_product.sku if self.selected_product else None
        self.raise_fault(code, sku=sku, outcome=outcome)
        # Captured BEFORE _fail_vend: it runs the vend_failed transition,
        # whose before-hook (on_vend_failed) fully clears self._sale (see
        # its own comment) before this call returns -- notifying with a
        # None sale afterward would be useless to any observer.
        sale = self._sale
        self._fail_vend(code, outcome=outcome)
        if sale is not None:
            if outcome == "no_report":
                # Kept as its own outcome ("timeout"), distinct from
                # "vend_failed", even though it runs through the same
                # vend_failed/PAY-102 transition above (system-tests
                # design §2.3; see controller/test_sale.py's TestSaleResult
                # docstring) -- DispenseCycle._timed_out's own outcome string.
                self._notify_settled(sale, "timeout", None)
            else:
                self._notify_settled(sale, "vend_failed", code.value)

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
            vend_log.info(f"DISPENSER: slot {slot}, state: {state}")
            return

        if self.state != "dispensing":
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' outside dispensing "
                f"state (current state: {self.state}, slot {slot})"
            )
            return
        if self._cycle is None:
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' with no active "
                f"dispense cycle (slot {slot})"
            )
            return

        report = self._cycle.classify(data)
        if report is None:
            # Logged by classify itself (intermediate step, slot mismatch,
            # or request_id mismatch) -- nothing left to do here.
            return

        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        if report.success:
            # Cancel the dispense_timeout timer here, before the
            # test/production split and its own await (`_cycle.record()`
            # suspends on a real asyncio.to_thread call). Once the board has
            # reported a successful dispense the customer already has the
            # product, so a `dispense_timeout` that fires during that
            # suspension must never be allowed to fail this settled sale --
            # on_dispense_failed's guards (`cycle is self._cycle` and
            # `state == "dispensing"`) would otherwise both still pass,
            # restoring the price to escrow and possibly issuing a refund
            # for a sale that already succeeded. _finish_dispensing's own
            # cancel() below is idempotent and stays as a backstop.
            self._cycle.cancel()
            txn_log.info(f"DISPENSE SUCCESS: slot {slot}, product '{product_name}'")
            vend_log.info(f"DISPENSE COMPLETE: slot {slot}, product '{product_name}'")
            if self._sale is not None and self._sale.is_test:
                # is_test lives on the sale (self._sale.is_test, set only
                # by run_test_sale), not on the maintenance lease -- so a
                # lease release or idle-timeout mid-run cannot flip this
                # sale to production (system-tests design §2.3). Neither a
                # `sale` row nor a `dispense` event is written; run_test_sale itself writes
                # the `test_run` event once it observes this outcome via
                # the sale-settled notification below.
                # DispenseCycle.record's own bookkeeping (clearing
                # pending_sale_shares) is replicated here since it is
                # skipped entirely for a test sale.
                self._sale = self._sale.with_(shares=None)
            else:
                recorded = await self._cycle.record()
                if recorded and self._sale is not None:
                    self._sale = self._sale.with_(shares=None)
                # A `False` return is the PAY-104 path: shares stay so the
                # on-disk session snapshot remains the sale's only record.
            if report.outcome is DispenserOutcome.door_open:
                # door_open is a customer success (the ice/water was
                # released) but a hardware fault in its own right -- the
                # trap door failed to close, which is why ICE-402 is a
                # PAYMENT_BLOCKING_FAULTS member. Raised after recording the
                # sale (the customer did get their product) and before
                # _finish_dispensing, matching the "success path plus a
                # fault" shape the brief calls for.
                sku = self.selected_product.sku if self.selected_product else None
                self.raise_fault(
                    FaultCode.ICE_402, sku=sku, outcome=report.outcome.value
                )
            # Captured BEFORE _finish_dispensing: it runs the
            # complete_transaction transition, whose before-hook
            # (on_complete_transaction) clears self._sale (via
            # selected_product = None) before this call returns. Fired
            # AFTER _finish_dispensing, not before, so every one of the
            # three settled-notification sites observes the same thing:
            # an already-settled FSM, never 'dispensing' (decision 1).
            sale = self._sale
            self._finish_dispensing()
            if sale is not None:
                self._notify_settled(sale, "dispensed", None)
            return

        self._cycle.cancel()
        code = report.fault
        sku = self.selected_product.sku if self.selected_product else None
        txn_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {report.outcome.value}, fault: {code.value}"
        )
        vend_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {report.outcome.value}, fault: {code.value}"
        )
        self.raise_fault(code, sku=sku, outcome=report.outcome.value)
        # Captured BEFORE _fail_vend: it runs the vend_failed transition,
        # whose before-hook (on_vend_failed) fully clears self._sale (see
        # its own comment) before this call returns -- notifying with a
        # None sale afterward would be useless to any observer.
        sale = self._sale
        self._fail_vend(code, outcome=report.outcome.value)
        if sale is not None:
            self._notify_settled(sale, "vend_failed", code.value)

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

    # --- Observers (state-change and sale-settled subscriptions) ---
    #
    # Two independent subscriber lists: state-change (every FSM
    # transition) and sale-settled (one of the three terminal outcomes
    # below). Callbacks run synchronously, in subscription order; a
    # raising callback is logged and never stops the rest. Each subscribe
    # method returns an unsubscribe closure rather than requiring the
    # caller to keep the callback reference around to remove it later.

    def subscribe_state_change(self, cb: Callable[[str], None]) -> Callable[[], None]:
        """Subscribe to every FSM state transition (fired from
        `_after_state_change`, after `self._outputs.state_changed`).
        Returns a zero-arg unsubscribe closure; calling it more than once
        is harmless."""
        self._state_change_observers.append(cb)

        def _unsubscribe() -> None:
            if cb in self._state_change_observers:
                self._state_change_observers.remove(cb)

        return _unsubscribe

    def subscribe_sale_settled(
        self, cb: Callable[[SaleContext, str, str | None], None]
    ) -> Callable[[], None]:
        """Subscribe to a sale settling -- fired from exactly three sites
        (`on_dispenser_event`'s success and failure branches,
        `on_dispense_failed`) with `(sale, outcome, fault_code)`, `sale`
        captured before the FSM callback that settled it
        (`on_vend_failed`/`_finish_dispensing`, via `complete_transaction`)
        clears `self._sale`. Returns a zero-arg unsubscribe closure;
        calling it more than once is harmless."""
        self._sale_settled_observers.append(cb)

        def _unsubscribe() -> None:
            if cb in self._sale_settled_observers:
                self._sale_settled_observers.remove(cb)

        return _unsubscribe

    def _notify_settled(
        self, sale: SaleContext, outcome: str, fault_code: str | None
    ) -> None:
        """Run every sale-settled observer with `(sale, outcome,
        fault_code)`, in subscription order; an exception from one is
        logged and never stops the rest."""
        for cb in list(self._sale_settled_observers):
            try:
                cb(sale, outcome, fault_code)
            except Exception:
                logger.exception("sale-settled observer raised")

    # --- FSM Callback Methods ---
    def _after_state_change(self, *args, **kwargs):
        """Runs after every FSM transition with self.state already updated.

        Accepts and ignores *args/**kwargs — the ``transitions`` library
        forwards whatever arguments the trigger was called with (e.g.
        ``vend_failed(code=..., outcome=...)``) to every callback list,
        including ``after_state_change``.
        """
        self._outputs.state_changed(self.state)
        for cb in list(self._state_change_observers):
            try:
                cb(self.state)
            except Exception:
                logger.exception("state-change observer raised")

    @logger.catch()
    def on_start_interaction(self):
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to interacting_with_user for product: {self.selected_product}"
        )
        self._reset_session_timeout()
        self._outputs.display("interacting_with_user")
        self._outputs.refresh()
        self.send_customer_message(
            "Interaction started. Please insert funds or select a product."
        )

    @logger.catch()
    def on_dispense_product(self):
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to dispensing for product: {self.selected_product}"
        )
        self._cancel_session_timeout()
        self._outputs.display("dispensing")
        self._outputs.refresh()
        self.send_customer_message(
            "Processing your payment and dispensing your product..."
        )
        # Tell the vending ESP32 which slot to dispense, through the command
        # dispatcher, carrying the slot's whole validated profile so a
        # dispensers.toml save mid-vend cannot affect this in-flight
        # command. Use the product's own stable `slot` field, NOT its
        # position in self.products — deleting an earlier product from the
        # catalog shifts list indices but must not change which physical
        # motor/slot a remaining product dispenses from. The rest of the
        # dispatch -- profile lookup, the CFG-101 fallback, request_id
        # minting, the dispense command, the timeout timer, and the actual
        # send -- lives in DispenseCycle (controller/dispense_cycle.py).
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
        logger.info(
            f"{STATE_CHANGE_PREFIX} Completing transaction. Remaining escrow: ${self.credit_escrow:.2f}"
        )
        dest = self._post_dispense_dest()
        # Clear the completed selection now — a stale reference here is what let a
        # late/duplicate MQTT dispenser fault (jammed/error, QoS 0, no dedup) issue a
        # bogus refund at the old product's price. The state guard in
        # on_dispenser_event is the primary fix; clearing here removes the stale
        # data too. Nothing downstream needs selected_product to persist across a
        # completed sale — a customer with remaining credit picks a fresh product via
        # select_product(), which overwrites it unconditionally.
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
        logger.info(
            f"{STATE_CHANGE_PREFIX} Resetting to idle state. Previous selection: {self.selected_product}"
        )
        # Minor fix M3: an abnormal exit from 'dispensing' (reset/error)
        # must clear per-sale dispatch context the same way a normal
        # finish or a failed vend does, so a stray late hardware report or
        # dispatch failure for the sale that was in flight can't act on
        # stale mechanism/request_id state after the reset. Setting
        # selected_product to None clears the whole SaleContext (product,
        # shares, mechanism, request_id, is_test together), so there is
        # nothing left to clear separately afterward.
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        self.selected_product = None
        self.last_insufficient_message = ""
        self._outputs.display("idle")
        self._outputs.refresh()

    @logger.catch()
    def on_cancel_sale(self):
        """Cancel a live session without treating it as a hardware/system error.

        Mirrors the cleanup ``_expire_session`` performs (refund escrow, clear the
        selection, cancel timers, notify, publish/update/refresh) but for the case
        where the selected product was deleted from the catalog out from under an
        in-progress customer session. Unlike ``on_error`` this does not park the
        machine in ``error`` — a benign catalog edit shouldn't take the whole VMC
        offline until an admin reset.
        """
        logger.info(
            f"{STATE_CHANGE_PREFIX} Cancelling sale for product: {self.selected_product}. "
            "Returning to idle without entering the error state."
        )
        txn_log.info("SALE CANCELLED: selected product removed from catalog")
        self._cancel_session_timeout()
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        self.request_refund(reason="cancel")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._outputs.display("idle")
        self._outputs.refresh()
        self.send_customer_message(
            "Sorry, the selected product is no longer available. Please make a new selection."
        )

    @logger.catch()
    def on_vend_failed(self, code: FaultCode, outcome: str):
        """`before` hook for dispensing -> interacting_with_user on a failed vend.

        Restores the price to escrow (it was deducted in process_payment),
        records the failure, and clears the selection. Whether the customer
        stays to choose again or is paid out is decided in _fail_vend.

        The restore must never reclassify money: it re-credits exactly the
        per-method shares `EscrowLedger.consume_fifo` consumed for this sale
        (pending_sale_shares), as separate Credits, not one blob of the
        current/default method. That is what stops a failed vend laundering
        cash into card (or any other method) in the sales ledger.

        The restored total is likewise derived from those shares, not a
        fresh `product.price` read: `selected_product` is the *live*
        catalog object (services/config_store.update_product mutates it in
        place), so an operator can edit the price while this sale is
        mid-dispense. Re-crediting the edited price here would both credit
        the wrong amount to escrow and report it in the failure event, the
        transaction log and the customer message below.
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
            # Should be unreachable: on_vend_failed only runs from
            # dispensing, which is only entered right after
            # `EscrowLedger.consume_fifo` sets pending_sale_shares. Guard, not a
            # path — attribute to "unknown" rather than guess a method.
            price = product.price if product else 0.0
            logger.warning(
                "on_vend_failed: no pending_sale_shares recorded; crediting "
                f"${price:.2f} back to escrow as 'unknown'"
            )
            shares = {"unknown": round(price, 2)}
        else:
            price = round(sum(shares.values()), 2)
        now = time.time()
        self._escrow.restore(shares, now, price)
        logger.error(
            f"{STATE_CHANGE_PREFIX} Vend failed for '{name}' ({code.value}, {outcome}); "
            f"${price:.2f} returned to escrow"
        )
        txn_log.error(
            f"VEND FAILED: '{name}' {code.value} ({outcome}); ${price:.2f} returned to escrow"
        )
        recorder = self._recorder()
        if recorder and not (self._sale is not None and self._sale.is_test):
            # Copilot review (PR 22, id=4128088653): on_vend_failed is the
            # one place that runs for every failed/timed-out vend,
            # production or test (both on_dispenser_event's failure
            # branch and on_dispense_failed reach it through _fail_vend ->
            # the vend_failed transition -> this hook). Recording
            # unconditionally here counted a failed or timed-out simulated
            # sale in EventRecorder.get_summary()'s vends_failed KPI,
            # contradicting the guarantee that a test sale moves nothing.
            # run_test_sale's own test_run row (system-tests design §4) is
            # what should represent this run, not a second, KPI-visible
            # vend_failed row -- so this is skipped for a test sale while
            # everything else in this method (restoring escrow, the
            # customer message) still runs exactly as it does for a real
            # vend.
            recorder.record(
                "vend_failed",
                value=price,
                metadata={"code": code.value, "sku": sku, "outcome": outcome},
            )
        self.selected_product = None
        self.last_insufficient_message = ""
        self.send_customer_message(
            f"Sorry, {name} could not be dispensed ({code.value}). "
            f"Your ${price:.2f} credit has been kept."
        )

    def _fail_vend(self, code: FaultCode, outcome: str) -> None:
        """Run the vend_failed transition, then decide: choose again, or pay out."""
        # Captured BEFORE vend_failed: its before-hook (on_vend_failed)
        # fully clears self._sale at its end, so reading is_test off
        # self._sale any later in this method would always see None/False.
        is_test = self._sale is not None and self._sale.is_test
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        self.vend_failed(code=code, outcome=outcome)
        if is_test:
            # on_vend_failed just restored this test sale's synthetic
            # "test" credit to escrow -- unconditionally, whether or not
            # run_test_sale is still around to observe the settlement (it
            # may have been cancelled while this vend was in flight; Task
            # 9 made this unconditional rather than checking for a waiter
            # that no longer exists). Test money must never sit on the
            # machine or leave via a refund command (system-tests design
            # §2.3): clear it here, directly, never through request_refund.
            cleared = self._escrow.take_all()
            logger.debug(
                f"test credit cleared directly, never refunded (${cleared:.2f}, "
                f"{code.value}, {outcome})"
            )
        if not self._sellable_products():
            txn_log.info("No sellable products remain; refunding and returning to idle")
            # A test sale's price, just restored to escrow by on_vend_failed
            # above, is not real money and must never leave via a real
            # refund command (system-tests design §2.3: "escrow is cleared
            # without a refund command"). run_test_sale clears it directly
            # once the run's outcome is known. is_test lives on the sale,
            # captured above before on_vend_failed cleared it, so this
            # still reads correctly even if the lease has since been
            # released.
            if not is_test:
                self.request_refund(reason=code.value)
            self._cancel_session_timeout()
            self.machine.set_state("idle")
            self._outputs.state_changed(self.state)
            self._outputs.display("idle")
        else:
            self.send_customer_message("Please choose another product.")
            self._reset_session_timeout()
            self._outputs.state_changed(self.state)
            self._outputs.display("interacting_with_user")
        self._outputs.refresh()

    @logger.catch()
    def on_error(self):
        logger.error(
            f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state."
        )
        recorder = self._recorder()
        if recorder:
            recorder.record("error", value=1.0)
        # Minor fix M3: see on_reset's comment above -- same reasoning.
        # Unlike on_reset, on_error does NOT clear selected_product (the
        # log line above still prints it), so this is a genuine partial
        # clear via with_ rather than the full self._sale = None on_reset
        # uses -- guarded because error_occurred can fire from any state,
        # including idle with no sale in progress at all.
        if self._cycle is not None:
            self._cycle.cancel()
        self._cycle = None
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        # Pay out any remaining credit through the gateway
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
    @logger.catch()
    def deposit_funds(self, amount, payment_method="Simulated Payment"):
        logger.debug(f"Depositing funds: amount={amount:.2f}, method={payment_method}")
        if amount <= 0:
            logger.warning(f"Ignoring non-positive deposit: {amount}")
            return
        if self._in_maintenance() and payment_method != "test":
            # Payment is disabled for the whole lease (SVC-102 blocks it via
            # availability), so this only covers the race between the
            # disable command and a coin already in the mechanism -- still
            # a customer's money, so it goes straight back out rather than
            # into escrow, where it could otherwise become spendable after
            # the hold ends. The one exception is `run_test_sale`'s own
            # credit, deposited with method "test" -- the `and
            # payment_method != "test"` guard above lets it fall through to
            # the normal escrow path below instead of being refunded; it is
            # the only credit accepted during a lease (system-tests design
            # §2.3).
            #
            # Trust boundary (round-1 review, minor finding 2): payment_method
            # here is `PaymentEvent.method`, a raw string arriving over MQTT
            # from the trusted payment subsystem -- not authenticated
            # end-to-end. Anyone who can already publish on that broker could
            # spoof method="test" during a lease and have their credit sit in
            # escrow instead of being auto-refunded. This is deliberately
            # NOT a free-product path: whether a sale gets recorded to the
            # ledger is gated on `self.sale.is_test` (set only inside
            # run_test_sale, never by this string) and `pending_sale_for_
            # recovery()`'s own `is_test` check -- a spoofed "test" deposit
            # can sit unrefunded in escrow, but it cannot make a real sale
            # skip the sales table, and it cannot make a test sale post to
            # it either. Exploiting it already requires control of the
            # trusted MQTT bus, at which point far worse is possible, so
            # this is intentionally left as-is rather than redesigned.
            logger.warning(
                f"Credit ${amount:.2f} arrived during a maintenance lease; "
                "refunding rather than escrowing"
            )
            self._escrow.deposit(payment_method, amount, time.time())
            self.last_payment_method = payment_method
            self.request_refund(reason="maintenance")
            return
        availability = self._availability()
        if availability and not availability.payment_enabled:
            logger.warning(
                f"Credit ${amount:.2f} arrived while payment is disabled "
                f"({', '.join(availability.payment_blocking_reasons())}); "
                "escrowed"
            )
        self._escrow.deposit(payment_method, amount, time.time())
        self.last_payment_method = payment_method
        logger.info(
            f"Deposited ${amount:.2f} via {payment_method}. New escrow: ${self.credit_escrow:.2f}"
        )
        # Arm/reset the safety-net timer on every deposit, not only once the
        # FSM has reached interacting_with_user. Money can arrive (via the
        # MDB gateway over MQTT) while still idle — before any button press,
        # or while a soft fault (e.g. vending offline) is refusing every
        # selection — and on_start_interaction's own message ("Please insert
        # funds or select a product") already treats deposit-before-selection
        # as a normal entry point. Without arming here, escrow taken while
        # idle would never be refunded: see _expire_session's idle branch.
        self._reset_session_timeout()
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
        logger.info(
            f"Refund of ${amount:.2f} requested via {self.last_payment_method} "
            f"(reason={reason}, request_id={pending.request_id})"
        )
        txn_log.info(
            f"REFUND REQUESTED: ${amount:.2f} via {self.last_payment_method} "
            f"reason={reason} request_id={pending.request_id}"
        )
        self.send_customer_message(
            f"Refund of ${amount:.2f} requested via {self.last_payment_method}. "
            "Please wait..."
        )
        self._outputs.refresh()

    async def on_refund_ack(self, topic: str, data: dict):
        """Payment gateway acknowledged (or refused) a refund command."""
        result = PaymentRefundResult.model_validate(data)
        self._refunds.handle_ack(result)

    def _refund_confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._outputs.persist()
        txn_log.info(
            f"REFUND CONFIRMED: ${amount_returned:.2f} request_id={pending.request_id}"
        )
        recorder = self._recorder()
        if recorder:
            recorder.record(
                "refund",
                value=amount_returned,
                metadata={"request_id": pending.request_id, "reason": pending.reason},
            )
        self.send_customer_message(
            f"Refund of ${amount_returned:.2f} issued via {self.last_payment_method}."
        )

    def _refund_failed(self, pending: PendingRefund, detail: str) -> None:
        self._outputs.persist()
        txn_log.error(
            f"REFUND FAILED: ${pending.amount:.2f} request_id={pending.request_id} "
            f"reason={pending.reason} detail={detail}"
        )
        recorder = self._recorder()
        if recorder:
            recorder.record(
                "refund_failed",
                value=pending.amount,
                metadata={
                    "request_id": pending.request_id,
                    "reason": pending.reason,
                    "detail": detail,
                },
            )
        self.send_customer_message(
            f"We could not return ${pending.amount:.2f} automatically. "
            f"Please contact support and quote {pending.request_id[:8]}."
        )
        self.raise_fault(FaultCode.PAY_103, outcome=detail)

    # --- Maintenance Lease (system-tests design §2.2) ---
    #
    # The lease lifecycle (the hold, its idle timer, the standby sweep,
    # grant/release/takeover/run accounting) AND every FSM precondition it
    # needs (`begin_maintenance`/`begin_standby`/`end_maintenance`/
    # `take_over_maintenance`) now live entirely on the `MaintenanceLease`
    # collaborator -- the Machine composition root (controller/machine.py)
    # owns it, and tests/routes
    # read and mutate it as `machine.lease` directly. The one FSM operation
    # the lease cannot do for itself -- making the machine idle (refund the
    # right amount, cancel a live sale, cancel the session timer) -- stays
    # here as `make_idle_for_service`, which `MaintenanceLease.begin_standby`
    # calls back through the `make_idle_for_service` callable `Machine`
    # wires in.

    def make_idle_for_service(self) -> bool:
        """Make the machine idle for `MaintenanceLease.begin_standby`
        (system-tests design §2.2a): the one FSM operation the lease needs
        and cannot do itself.

        Refuses only while `dispensing` (a running motor is never
        aborted) -- returns False and touches nothing. Otherwise refunds
        whatever credit is on the machine (``reason="maintenance"``),
        cancels a live customer sale (`interacting_with_user`) or the idle
        session timer (`idle`), and returns True.
        """
        if self.state == "dispensing":
            return False
        if self.state == "interacting_with_user":
            # request_refund is a no-op at zero escrow (just a customer
            # message), so this is safe to call unconditionally; it also
            # means on_cancel_sale's own request_refund(reason="cancel")
            # call below finds nothing left to refund -- exactly one
            # refund happens, with reason="maintenance".
            self.request_refund(reason="maintenance")
            self.cancel_sale()
        elif self.state == "idle":
            if not self._escrow.is_empty_within_tolerance:
                self.request_refund(reason="maintenance")
            self._cancel_session_timeout()
        elif self.state == "error":
            if not self._escrow.is_empty_within_tolerance:
                self.request_refund(reason="maintenance")
        return True

    def find_product(self, sku: str) -> tuple[int | None, Product | None]:
        """Return ``(button_index, product)`` for `sku` in the live catalog,
        or ``(None, None)``. Looked up by identity match against
        ``self.products`` (not ``list.index``, which compares by value and
        could pick the wrong entry for two otherwise-identical products).

        Public -- was `_find_product_by_sku`, renamed with no change in
        behavior so `controller/test_sale.py`'s `TestSaleRunner.
        run_test_sale` can call it without reaching into a VMC-private
        method."""
        for index, product in enumerate(self.products):
            if product.sku == sku:
                return index, product
        return None, None

    def begin_test_sale(self, product: Product) -> bool:
        """Seed and select a simulated sale for `product` (system-tests
        design §2.3). Refuses a second, overlapping call outright:
        there is no ``await`` between the guard check and setting the
        flag, so under asyncio's single-threaded event loop the
        check-and-set is atomic -- no other coroutine can run between them
        and slip past the guard (see `run_test_sale`'s own comments for
        why `run_id` uniqueness alone would not do this).

        Seeds `self._sale` with `is_test=True` *before* `select_product`
        runs -- `select_product`'s own availability check (the
        `test_sale_sellable` vs. `product_sellable` branch) reads
        `self.sale.is_test` before the product is technically "selected",
        so `is_test` cannot wait for `select_product`'s own assignment to
        create the context. The `selected_product` setter's "same product
        -> keep the existing context" rule is what lets `select_product`'s
        own `self.selected_product = candidate` leave this seeded context
        (and its `is_test=True`) alone rather than replacing it.

        Returns `True` once `product` is genuinely selected and the FSM
        has reached `interacting_with_user`; `False` otherwise (locked
        out, unavailable, or sold out) -- the caller (`run_test_sale`)
        turns a `False` into its own "could not select" `RuntimeError`.
        Callers must pair a call with `end_test_sale()`, in a `finally`,
        regardless of which way this returns or whether an exception
        reaches that `finally` instead.

        Exception-safe: anything raised between setting
        `_test_sale_in_progress = True` and this method's own return --
        `deposit_funds`, `find_product`, or `select_product` -- clears
        the flag and the seeded `SaleContext` before propagating, taking
        the just-deposited "test" credit off escrow directly (never
        through `request_refund`, same as `end_test_sale`). A leaked flag
        here would never be cleared by `end_test_sale` (its own `began`
        guard in `TestSaleRunner.run_test_sale` skips calling it when
        `begin_test_sale` itself raised), which would wrongly refuse every
        later `run_test_sale` call as "already in progress" forever, and
        -- the money-safety half of this -- would make `_snapshot()` mark
        a later, genuinely production sale's crash snapshot
        `is_test=True`, silently hiding it from `PAY-104` recovery.
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
        """Release `begin_test_sale`'s guard and clean up after one
        simulated sale, whatever the outcome.

        Still `dispensing` means this call's own task was cancelled (or
        is otherwise returning) while the hardware dispense is still
        physically in flight -- the context is left exactly as it is, so
        the eventual hardware report still settles it as a test sale
        (never `DispenseCycle.record`, never a real refund of the
        synthetic "test" credit -- `_fail_vend` clears that credit itself
        once the vend does settle). Any other state means the sale
        already settled (or was refused before ever reaching
        `dispensing`), so the leftover context is cleared here.

        Escrow is always cleared directly, never through
        `request_refund` -- test money is not real money and this is not
        a refund (system-tests design §2.3); a no-op when a successful
        dispense already left escrow at zero.
        """
        if self.state != "dispensing":
            self._sale = None
        self._escrow.take_all()
        self._test_sale_in_progress = False

    @logger.catch()
    def initiate_virtual_payment(self, amount):
        """
        Initiates a virtual payment by generating a payment URL and corresponding QR code.
        Cycles through available virtual payment gateways.
        """
        prompt = self.payment_gateway_manager.next_payment_prompt(amount)
        if prompt is None:
            self.send_customer_message("Virtual payment is currently unavailable.")
            return

        current_gateway, qr_image = prompt
        self._outputs.show_qr(qr_image)
        self.send_customer_message(
            f"Virtual Payment Option ({current_gateway}): Scan the QR code above."
        )

    @logger.catch()
    def select_product(self, product_index):
        logger.debug(f"Selecting product with index: {product_index}")
        if self.state not in ["idle", "interacting_with_user"]:
            logger.warning("Cannot change selection; machine not ready.")
            return
        if not (0 <= product_index < len(self.products)):
            logger.error(f"Invalid product index: {product_index}")
            return

        # `product_index` here is the physical button index (ButtonPress.button),
        # not the product's dispense `slot` — buttons stay positional for now.
        candidate = self.products[product_index]
        locked_code = self._faults.lockouts.get(candidate.sku)
        if locked_code is not None:
            txn_log.info(
                f"LOCKED OUT: '{candidate.name}' ({locked_code.value}), customer rejected"
            )
            self.send_customer_message(
                f"{candidate.name} is unavailable ({locked_code.value}). "
                "Please choose another product."
            )
            return

        availability = self._availability()
        if availability:
            # A maintenance test sale is exempt from the sale-blocking
            # effect of its OWN SVC-102 lease fault -- and of SVC-102
            # alone -- because test-ness lives on the sale
            # (self.sale.is_test, set only by run_test_sale, which seeds
            # the SaleContext with is_test=True before this method even
            # runs), not on the lease: a real customer press reaching this
            # method during a lease has no seeded context yet (self._sale
            # is None, or belongs to a different, already-cleared sale) and
            # is still refused by product_sellable like any other
            # safety-blocked sale.
            if self._sale is not None and self._sale.is_test:
                sellable, failing = availability.test_sale_sellable(candidate)
            else:
                sellable, failing = availability.product_sellable(candidate)
            if not sellable:
                reason = failing[0]
                txn_log.info(
                    f"UNAVAILABLE: '{candidate.name}' blocked by {reason}, customer rejected"
                )
                self.send_customer_message(
                    f"{candidate.name} is unavailable right now ({reason}). "
                    "Please try again later."
                )
                return

        self.selected_product = candidate
        logger.info(
            f"Selected product: {self.selected_product.name} at ${self.selected_product.price:.2f}"
        )
        txn_log.info(
            f"PRODUCT SELECTED: '{self.selected_product.name}' (${self.selected_product.price:.2f}), button {product_index}"
        )
        vend_log.info(
            f"PRODUCT SELECTED: '{self.selected_product.name}' (${self.selected_product.price:.2f}), button {product_index}"
        )

        inventory = self._inventory()
        if inventory and not inventory.is_available(self.selected_product.sku):
            logger.error(f"{self.selected_product.name} is sold out.")
            txn_log.info(f"SOLD OUT: '{self.selected_product.name}', customer rejected")
            self.send_customer_message(
                f"{self.selected_product.name} is sold out. Please select another product."
            )
            return

        if self.state == "idle":
            self.start_interaction()
            self._schedule(1.0, self.process_payment, label="process_payment")
        elif self.state == "interacting_with_user":
            self.initiate_virtual_payment(self.selected_product.price)
            self._schedule(1.0, self.process_payment, label="process_payment")
        self._outputs.refresh()

    @logger.catch()
    def _update_selection_message(self):
        price = self.selected_product.price if self.selected_product else 0
        if self.selected_product:
            if self.credit_escrow < price:
                required = price - self.credit_escrow
                message = f"Changed selection to {self.selected_product.name}. Insert additional ${required:.2f}."
            else:
                message = f"Changed selection to {self.selected_product.name}. Sufficient funds available."
        else:
            message = "No product selected."
        logger.debug(f"Updated selection message: {message}")
        self.send_customer_message(message)
        self.last_insufficient_message = message

    @logger.catch()
    def process_payment(self):
        logger.debug(f"Processing payment for product: {self.selected_product}")
        if self.state != "interacting_with_user":
            logger.debug(
                "State is not interacting_with_user; aborting payment process."
            )
            return

        if self.selected_product is None or self.selected_product not in self.products:
            logger.error(
                "Selected product no longer exists in the catalog; cancelling sale."
            )
            self.cancel_sale()
            return

        price = self.selected_product.price if self.selected_product else 0
        if self.credit_escrow >= price:
            logger.info(
                f"{STATE_CHANGE_PREFIX} Escrow sufficient ({self.credit_escrow:.2f} >= {price:.2f}). Processing payment."
            )
            txn_log.info(
                f"PAYMENT SUFFICIENT: ${self.credit_escrow:.2f} >= ${price:.2f} for '{self.selected_product.name}', charging ${price:.2f}"
            )
            self.send_customer_message(
                "Sufficient funds received. Processing your payment..."
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
            # Dispenser hardware reports a terminal DispenserOutcome via MQTT;
            # no report within the timeout is a failed vend (PAY-102) -- the
            # timer itself is armed by the DispenseCycle built and started
            # inside dispense_product()'s on_dispense_product before-hook.
            self.last_insufficient_message = ""
        else:
            required = price - self.credit_escrow
            message = (
                f"Insufficient funds. Please insert an additional ${required:.2f}."
            )
            if message != self.last_insufficient_message:
                logger.info(message)
                txn_log.info(
                    f"PAYMENT INSUFFICIENT: ${self.credit_escrow:.2f} < ${price:.2f} for '{self.selected_product.name}', need ${required:.2f} more"
                )
                self.send_customer_message(message)
                self.last_insufficient_message = message
            self._schedule(5.0, self.process_payment, label="process_payment")

    def _reset_session_timeout(self):
        """Reset (or start) the customer session inactivity timer."""
        if self._session_timeout_task and not self._session_timeout_task.done():
            self._session_timeout_task.cancel()
        self._session_timeout_task = self._schedule(
            self.session_timeout_seconds, self._expire_session, label="session_timeout"
        )

    def _cancel_session_timeout(self):
        """Cancel the session timeout (e.g., when dispensing starts)."""
        if self._session_timeout_task and not self._session_timeout_task.done():
            self._session_timeout_task.cancel()
        self._session_timeout_task = None

    @logger.catch()
    def _expire_session(self):
        """Called when the session-timeout timer fires.

        Three cases need the stranded-money guarantee to hold:

        - ``interacting_with_user``: the customer walked away mid-session
          (selected a product, or not) — unconditionally refund and reset,
          exactly as before this fix. Handled even at zero escrow, so a
          lingering selection is still cleared.
        - ``idle`` with escrow > 0: money arrived (MQTT payment event) but
          every selection attempt was refused before the FSM ever left idle
          (e.g. the vending subsystem was offline) — or none was attempted
          at all. Refund and stay idle; nothing else to reset.
        - ``error`` with escrow > 0: ``on_error`` already refunds whatever
          credit was present *at the moment it ran*, but ``deposit_funds``
          arms/re-arms this same timer on every deposit regardless of FSM
          state (see its docstring), so a credit that arrives *after*
          ``on_error`` — the machine is still parked in ``error`` awaiting an
          admin ``reset_state`` — is added to escrow with nothing left to
          refund it: ``on_reset`` doesn't touch ``credit_escrow`` either.
          Refund and stay in ``error``; the admin still has to clear the
          fault that put the machine there.

        ``dispensing`` is the only remaining no-op, and deliberately so: a
        vend in flight must never be refunded out from under the customer.
        A deposit that lands mid-vend still re-arms this timer, but by the
        time it could fire the vend has already resolved — on success,
        ``_post_dispense_dest`` lands on ``interacting_with_user`` whenever
        that deposit left escrow positive, which this method's own branch
        for that state then covers; on failure/timeout, ``_fail_vend``
        explicitly re-arms (products remain) or refunds and forces ``idle``
        (none do) before this timer could ever observe ``dispensing`` again.
        """
        if self.state == "idle":
            if self.credit_escrow <= 0:
                return
            logger.info(
                "Idle session timed out with stranded escrow; refunding "
                f"${self.credit_escrow:.2f}."
            )
            txn_log.info(
                f"SESSION TIMEOUT: refunding ${self.credit_escrow:.2f} (idle, no active selection)"
            )
            self.request_refund(reason="session_timeout")
            self.selected_product = None
            self.last_insufficient_message = ""
            self._outputs.state_changed(self.state)
            self._outputs.refresh()
            return
        if self.state == "error":
            if self.credit_escrow <= 0:
                return
            logger.info(
                "Session timed out in error state with stranded escrow; "
                f"refunding ${self.credit_escrow:.2f}."
            )
            txn_log.info(
                f"SESSION TIMEOUT: refunding ${self.credit_escrow:.2f} "
                "(error, deposited after on_error's own refund)"
            )
            self.request_refund(reason="session_timeout")
            self.selected_product = None
            self.last_insufficient_message = ""
            self._outputs.state_changed(self.state)
            self._outputs.refresh()
            return
        if self.state != "interacting_with_user":
            return
        logger.info("Customer session timed out due to inactivity.")
        txn_log.info(f"SESSION TIMEOUT: refunding ${self.credit_escrow:.2f}")
        self.request_refund(reason="session_timeout")
        self.selected_product = None
        self.last_insufficient_message = ""
        # Manually transition back to idle (reset_state only works from error)
        self.machine.set_state("idle")
        self._outputs.state_changed(self.state)
        self._outputs.display("idle")
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
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {product_name}")
        self.send_customer_message("Product dispensed. Enjoy your purchase!")
        inventory = self._inventory()
        if inventory and self.selected_product:
            sku = self.selected_product.sku
            if inventory.is_tracked(sku):
                inventory.decrement(sku, persist=False)
                self._tasks.fire_and_forget(inventory.save_async(), persistent=True)
                logger.info(
                    f"Inventory for {self.selected_product.name} updated: {inventory.get_count(sku)} remaining."
                )
        self.complete_transaction()
        self._outputs.persist()
        self._outputs.refresh()
