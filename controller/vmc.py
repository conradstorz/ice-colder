# controller/vmc.py
import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from uuid import uuid4
from transitions import Machine
from loguru import logger
from services.payment_gateway_manager import PaymentGatewayManager
from services.mqtt_messages import (
    VMCStatus,
    PaymentEvent,
    PaymentEnableCommand,
    ButtonPress,
    DispenseCommand,
    VMCAlert,
)
from contracts.vending_machine import (
    DispenserOutcome,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    SubsystemCapabilities,
    fault_for_outcome,
)
from config.config_model import ConfigModel, Product
from services.availability import Availability
from services.command_dispatcher import CommandTimeout
from services.health_monitor import HealthMonitor
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from services.session_store import Credit, SessionSnapshot, SessionStore
from services.event_recorder import SaleRecordingFailed
from services.dispensers import DispenserProfiles
from services.dispenser_schema import SlotProfile
from controller.fault_registry import FaultRegistry
from controller.escrow_ledger import EscrowLedger
from controller.refund_protocol import PendingRefund, RefundProtocol
from controller.sale_context import SaleContext
from controller.session_recovery import SessionRecovery
from controller.maintenance_lease import MaintenanceHold, MaintenanceLease
from controller.task_runner import TaskRunner
from controller.dispenser_gate import DispenserProfileGate
from controller import mqtt_inbound

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"


def _has_outcome_mapping(mechanism: str | None, outcome: DispenserOutcome) -> bool:
    """True iff `(mechanism, outcome)` has an entry in OUTCOME_FAULTS --
    used by `on_dispenser_event` (review finding C3) to decide
    whether `door_open` is a success for *this* mechanism (bagged_ice)
    or must take the ordinary failed-vend path (every other mechanism,
    e.g. water_fill, which has no `(water_fill, door_open)` mapping)."""
    try:
        fault_for_outcome(mechanism, outcome)
    except KeyError:
        return False
    return True


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

# Heartbeat loss per subsystem -> registry fault (ROADMAP §5, §8).
_LIVENESS_FAULTS = {
    "vending": FaultCode.COM_101,
    "ice_maker": FaultCode.COM_102,
    "mdb": FaultCode.PAY_101,
}


@dataclass
class TestSaleResult:
    """Outcome of one ``VMC.run_test_sale()`` run (system-tests design §2.3).

    ``path`` is the sequence of FSM states visited, in order, from the
    state the machine was in when the run started through to the state it
    settled in -- captured live via ``_after_state_change`` while
    ``_test_sale_path`` is not ``None``, not reconstructed after the fact.

    ``outcome`` is one of ``"dispensed"``, ``"vend_failed"``, ``"timeout"``.
    ``fault_code`` (a ``FaultCode.value`` string, e.g. ``"ICE-401"``) is set
    only when ``outcome == "vend_failed"`` -- it is always ``None`` for
    ``"dispensed"`` and for ``"timeout"``. A dispense timeout *internally*
    still runs the same ``vend_failed`` FSM transition with code
    ``PAY-102`` (see ``_dispense_timed_out``), but is kept as its own,
    distinct outcome here rather than folded into ``"vend_failed"``.

    ``run_id`` (Task 13b, system-tests design §4/§3) is the same id written
    onto the ``test_run`` log row's metadata below -- carrying it back on
    the result is what lets the Tests level's ``/tests/sale`` route render
    a Pass/Fail form that posts to ``/tests/runs/{run_id}/verdict`` without
    a second query, and is why a simulated sale's log row is verdictable at
    all (the pre-13b shape had no ``run_id``, so no verdict could ever be
    recorded against it).
    """

    sku: str
    path: list[str]
    outcome: str
    fault_code: str | None = None
    run_id: str | None = None


class VMC:
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

    # Maintenance lease (system-tests design §2.2).
    MAINTENANCE_IDLE_TIMEOUT_SECONDS = 300.0  # 5 minutes since last_activity_at
    MAINTENANCE_TAKEOVER_IDLE_SECONDS = (
        60.0  # lease must be idle this long to take over
    )
    # Standby lease (system-tests design §2.2a): how often the session
    # sweep re-checks the holder's web session liveness. A class attribute
    # so tests can shrink it rather than waiting out a real 30s interval.
    STANDBY_SWEEP_SECONDS = 30.0

    @logger.catch()
    def __init__(self, config: ConfigModel, *, tasks: TaskRunner | None = None):
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
        # dispatch request_id, and dispatch seq, all replaced wholesale at
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
        # never allowed to diverge (see _consume_credits_fifo's bug guard).
        # VMC.credit_escrow/escrow_credits below are read/write properties
        # aliasing self._escrow.total/self._escrow.credits, kept because
        # many existing tests read and write them directly.
        self._escrow = EscrowLedger()
        # Review finding I2: a monotonically increasing counter identifying
        # the *current* in-flight dispense dispatch. Incremented once per
        # on_dispense_product call (one dispatch attempt per entry into
        # 'dispensing'), which passes its own local `seq` straight into
        # `_persist_then_dispense` as a parameter (review finding M4) rather
        # than having that task re-read the live counter itself. A late
        # failure (e.g. a delayed CommandTimeout from a dispatch whose sale
        # has already settled and been superseded by a new one) is detected
        # by comparing that passed-in value against the live counter in
        # _fail_dispense_async, so it can never fail a different, later
        # sale than the one that actually dispatched.
        self._sale_seq: int = 0
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"

        self.update_callback = None
        self.message_callback = None
        self.qrcode_callback = None

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
        self._tasks = tasks if tasks is not None else TaskRunner()
        self._dispense_timeout_task: asyncio.Task | None = None
        self._session_timeout_task: asyncio.Task | None = None
        self._mqtt_client = None  # Set via set_mqtt_client()
        self._health_monitor: HealthMonitor | None = (
            None  # Set via set_health_monitor()
        )
        self._display_controller: DisplayController | None = (
            None  # Set via set_display_controller()
        )
        self._inventory: InventoryManager | None = (
            None  # Set via set_inventory_manager()
        )
        self._event_recorder = None  # Set via set_event_recorder()
        self._availability: Availability | None = None  # Set via set_availability()
        self._session_store: SessionStore | None = None  # Set via set_session_store()
        self._command_dispatcher = None  # Set via set_command_dispatcher()
        # Maintenance lease lifecycle (system-tests design §2.2/§2.2a):
        # the hold itself, its idle timer, and the standby session-liveness
        # sweep (controller/maintenance_lease.py). Deliberately not part of
        # any persisted snapshot -- see MaintenanceHold's docstring.
        # Exposed read-only as `self.lease` (VMC public surface design,
        # section 3); tests read `vmc.lease.hold`/`vmc.lease.idle_task`/
        # `vmc.lease.sweep_task` directly rather than a VMC-private alias.
        # `on_granted`/`on_released` are VMC callbacks (raise/clear
        # SVC-102); the three timing knobs are callables read at call time,
        # never snapshotted here, because tests set
        # `vmc.MAINTENANCE_IDLE_TIMEOUT_SECONDS`/
        # `vmc.MAINTENANCE_TAKEOVER_IDLE_SECONDS` on the live instance.
        self._lease = MaintenanceLease(
            schedule=self._schedule,
            on_granted=lambda: self.raise_fault(
                FaultCode.SVC_102, outcome="maintenance_lease_granted"
            ),
            on_released=lambda by: self.clear_fault(FaultCode.SVC_102.value, by=by),
            idle_timeout=lambda: self.MAINTENANCE_IDLE_TIMEOUT_SECONDS,
            takeover_idle=lambda: self.MAINTENANCE_TAKEOVER_IDLE_SECONDS,
            sweep_seconds=lambda: self.STANDBY_SWEEP_SECONDS,
        )
        # run_test_sale's own bookkeeping (system-tests design §2.3): the
        # Future its completion-handling call sites resolve with
        # (outcome, fault_code) once the sale settles, and the FSM states
        # visited while a test sale is in flight (appended by
        # _after_state_change). Both None whenever no test sale is running.
        self._test_sale_waiter: asyncio.Future | None = None
        self._test_sale_path: list[str] | None = None
        # True for the duration of exactly one run_test_sale call (set
        # before maintenance_test_run() is entered, cleared in that call's
        # own outer `finally`) -- the guard that refuses a second,
        # overlapping run_test_sale call from ever clobbering the single
        # _test_sale_waiter/_test_sale_path above. See run_test_sale's own
        # comments for why run_id uniqueness alone does not do this.
        self._test_sale_in_progress: bool = False
        self.subsystem_capabilities: dict[str, dict] = {}
        # Fault registry: product-scope faults by SKU, machine-scope faults
        # by code (controller/fault_registry.py). Exposed read-only as
        # `self.faults` (VMC public surface design, section 3); tests read
        # `vmc.faults.lockouts`/`vmc.faults.is_locked(...)`/`vmc.faults.has(...)`
        # directly rather than a VMC-private alias, and mutate only through
        # `raise_fault`/`clear_fault`, never the registry directly.
        self._faults = FaultRegistry(self._product_name)
        # Dispenser-profile gate: CFG-101/CFG-102 reconciliation against a
        # loaded DispenserProfiles (controller/dispenser_gate.py), the
        # eighth piece carved off the VMC god object. Set via
        # set_dispenser_profiles(); exposed read-only as `self.gate`, so
        # tests read `vmc.gate.profiles` directly rather than a VMC-private
        # alias.
        self._gate = DispenserProfileGate(
            products=lambda: self.config_model.products,
            is_locked=self._faults.is_locked,
            has_machine_fault=self._faults.has,
            raise_fault=self.raise_fault,
            clear_fault=lambda key, by: self.clear_fault(key, by=by),
        )
        # Refund protocol: request -> ack -> one retry -> terminal state
        # machine (controller/refund_protocol.py). Exposed read-only as
        # `self.refunds` (VMC public surface design, section 3); tests read
        # `vmc.refunds.pending` directly rather than a VMC-private alias.
        # The publish/schedule/ack_timeout/max_attempts callables are read
        # at call time, never snapshotted here -- see RefundProtocol's
        # docstring.
        self._refunds = RefundProtocol(
            publish=self._publish_refund_command,
            schedule=self._schedule,
            on_confirmed=self._refund_confirmed,
            on_failed=self._refund_failed,
            ack_timeout=lambda: self.REFUND_ACK_TIMEOUT,
            max_attempts=lambda: self.REFUND_MAX_ATTEMPTS,
        )
        # PAY-104 session recovery: the boot-time decision over a loaded
        # snapshot plus the read-only recovery accessor and its
        # record-once guards (controller/session_recovery.py). `store` is
        # read at call time (`self._session_store` is attached later by
        # `set_session_store`), never snapshotted here.
        self._recovery = SessionRecovery(
            store=lambda: self._session_store,
            product_name=self._product_name,
            pay104_active=lambda: self._faults.has(FaultCode.PAY_104),
        )
        # Telemetry-only MQTT inbound handlers (controller/mqtt_inbound.py).
        # `health`/`availability` are callables read at call time because
        # both are attached later via set_health_monitor/set_availability
        # and may be None in tests; `capabilities` is this VMC's own dict
        # object, not a copy, so the router's writes land exactly where
        # existing tests already read them (`vmc.subsystem_capabilities`).
        # The ICE-101 auto-clear and the vending-capabilities dispenser-
        # profiles reconcile stay VMC callbacks -- see TelemetryRouter's
        # docstring and _clear_ice101_lockouts/
        # _on_vending_capabilities_validated below.
        self._telemetry = mqtt_inbound.TelemetryRouter(
            health=lambda: self._health_monitor,
            availability=lambda: self._availability,
            capabilities=self.subsystem_capabilities,
            on_bin_half_full=self._clear_ice101_lockouts,
            on_capabilities_validated=self._on_vending_capabilities_validated,
        )
        self._start_time = time.monotonic()
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
        self.virtual_payment_index = 0

        logger.debug("VMC initialization complete.")

    def attach_to_loop(self, loop: asyncio.AbstractEventLoop):
        """Attach VMC to the running asyncio event loop. Must be called before scheduling."""
        self._tasks.attach(loop)
        logger.debug("VMC attached to asyncio event loop.")

    def cancel_pending_tasks(self):
        """Cancel all pending scheduled tasks. Call during shutdown.

        Persistence writes tracked by the task runner are never cancelled
        here — they are drained (awaited to completion) by
        ``drain_persistence()`` instead, so a shutdown cannot truncate an
        in-flight session/inventory save.
        """
        self._tasks.cancel_pending()
        self._cancel_dispense_timeout()
        self._cancel_session_timeout()
        self._refunds.cancel_all()
        logger.debug("VMC: all pending tasks cancelled.")

    @property
    def tasks(self) -> TaskRunner:
        """Read-only view of the event-loop task plumbing (VMC public
        surface design, section 3). `vmc.tasks.pending`/`.persist`/`.loop`
        are read directly; mutation only ever happens through a VMC method
        (`attach_to_loop`, `cancel_pending_tasks`, `drain_persistence`)."""
        return self._tasks

    def set_mqtt_client(self, client):
        """Attach an MQTTClient instance for publishing status and receiving events."""
        self._mqtt_client = client
        # Register handlers for inbound ESP32 messages. SUBSCRIPTIONS
        # (controller/mqtt_inbound.py) is the single source of truth for
        # which topics map to which VMC method, in the exact order the
        # individual client.register(...) calls used to run in.
        for topic, name in mqtt_inbound.SUBSCRIPTIONS:
            client.register(topic, getattr(self, name))
        logger.debug("VMC registered MQTT handlers.")

    def set_health_monitor(self, monitor: HealthMonitor):
        """Attach a HealthMonitor; its liveness transitions become COM/PAY faults."""
        self._health_monitor = monitor
        monitor.set_liveness_callback(self._on_subsystem_liveness)
        logger.debug("VMC attached health monitor.")

    def set_availability(self, availability: Availability):
        """Attach the permissive table; it publishes cmd/payment/enable through us."""
        self._availability = availability
        availability.set_fsm_state(self.state)
        availability.set_active_faults(self.active_faults())
        availability.set_publisher(self.publish_payment_enable)
        logger.debug("VMC attached availability.")

    def publish_payment_enable(self, accept: bool) -> None:
        """Sync publisher handed to Availability (fire-and-forget on the loop)."""
        if self._mqtt_client is None:
            logger.warning("No MQTT client; payment/enable not sent")
            return
        self._fire_and_forget(
            self._mqtt_client.publish(
                "cmd/payment/enable", PaymentEnableCommand(accept=accept)
            )
        )

    def _on_subsystem_liveness(self, subsystem: str, alive: bool) -> None:
        code = _LIVENESS_FAULTS.get(subsystem)
        if code is not None:
            if alive:
                self.clear_fault(code.value, by="auto")
            else:
                self.raise_fault(code, outcome="heartbeat_lost")
        if self._availability:
            self._availability.set_subsystem_alive(subsystem, alive)
            if subsystem == "mdb" and alive:
                self._availability.republish()

    def on_mqtt_connection(self, connected: bool) -> None:
        """Connection-state callback from MQTTClient (chained after the health monitor)."""
        if self._availability:
            self._availability.set_mqtt_connected(connected)
        if connected:
            self.clear_fault(FaultCode.COM_103.value, by="auto")
            if self._availability:
                self._availability.republish()
            self._publish_status()
        else:
            self.raise_fault(FaultCode.COM_103, outcome="disconnected")

    def set_display_controller(self, controller: DisplayController):
        """Attach a DisplayController so FSM state changes update the customer display."""
        self._display_controller = controller
        logger.debug("VMC attached display controller.")

    def set_inventory_manager(self, inventory: InventoryManager):
        """Attach an InventoryManager for persistent stock tracking."""
        self._inventory = inventory
        logger.debug("VMC attached inventory manager.")

    def set_event_recorder(self, recorder):
        """Attach an EventRecorder so FSM error events are persisted."""
        self._event_recorder = recorder
        logger.debug("VMC attached event recorder.")

    def set_session_store(self, store: SessionStore):
        """Attach the session store and evaluate any snapshot left by a previous run.

        Call after attach_to_loop, set_health_monitor and set_availability so
        the PAY-104 alert and the availability gate both land.
        """
        self._session_store = store
        decision = self._recovery.evaluate_at_boot()
        if decision.kind == "discard_test":
            logger.warning(
                "SessionStore: discarding a test-sale snapshot found at "
                f"boot (sku={decision.snapshot.selected_sku!r}, "
                f"state={decision.snapshot.state!r}); a test sale risks no "
                "real money, so no PAY-104 is raised."
            )
        elif decision.kind == "uncertain":
            self._flag_uncertain_session(decision.snapshot)
        logger.debug("VMC attached session store.")

    def set_command_dispatcher(self, dispatcher) -> None:
        """Attach the subsystem CommandDispatcher (system-tests design §2.1).

        A later task's `run_test_sale` and the Tests level routes use this
        to send actuator/automatic commands through the same dispatcher
        `main.py` registers on `cmd/+/ack`.
        """
        self._command_dispatcher = dispatcher
        logger.debug("VMC attached command dispatcher.")

    def set_dispenser_profiles(self, profiles: DispenserProfiles) -> None:
        """Attach the loaded `DispenserProfiles` -- delegates to
        `self._gate.attach` (controller/dispenser_gate.py)."""
        self._gate.attach(profiles)

    def dispenser_profile_for(self, product: Product) -> SlotProfile | None:
        """The one lookup every later task (dispense, Tests level, ...)
        uses -- delegates to `self._gate.profile_for`."""
        return self._gate.profile_for(product)

    def reconcile_dispenser_profiles(self) -> None:
        """Re-derive every product's CFG-101 lockout, and the machine's
        CFG-102 fault -- delegates to `self._gate.reconcile`."""
        self._gate.reconcile()

    def catalog_changed(self) -> None:
        """Tell the gate a product catalog mutation just landed --
        delegates to `self._gate.catalog_changed`."""
        self._gate.catalog_changed()

    # --- Read-only wired-service accessors (VMC public surface design,
    # section 3) --- each is attached via its own `set_*` method above;
    # tests and routes read the collaborator directly by these names
    # rather than a VMC-private attribute, and mutate only by calling the
    # matching `set_*` method (or, for `recovery`, never -- it is
    # read-only by construction).

    @property
    def session_store(self) -> SessionStore | None:
        return self._session_store

    @property
    def mqtt_client(self):
        return self._mqtt_client

    @property
    def command_dispatcher(self):
        return self._command_dispatcher

    @property
    def health_monitor(self) -> HealthMonitor | None:
        return self._health_monitor

    @property
    def availability(self) -> Availability | None:
        return self._availability

    @property
    def event_recorder(self):
        return self._event_recorder

    @property
    def display_controller(self) -> DisplayController | None:
        return self._display_controller

    @property
    def recovery(self) -> SessionRecovery:
        """Read-only PAY-104 session-recovery collaborator. See
        ``controller.session_recovery.SessionRecovery``."""
        return self._recovery

    def _flag_uncertain_session(self, snap: SessionSnapshot) -> None:
        detail = snap.error or (
            f"state={snap.state} escrow=${snap.credit_escrow:.2f} "
            f"sku={snap.selected_sku} refund={snap.pending_refund_request_id}"
        )
        logger.error(f"Transaction uncertain after restart: {detail}")
        txn_log.error(f"RESTART WITH OPEN SESSION: {detail}")
        if self._event_recorder:
            self._event_recorder.record(
                "session_uncertain", value=snap.credit_escrow, metadata=asdict(snap)
            )
        if self._availability:
            self._availability.set_transaction_certain(False)
        self.raise_fault(FaultCode.PAY_104, outcome=detail)

    def reconcile_session(self) -> None:
        """Future hook: query the payment gateway for held credit and clear
        PAY-104 automatically. The contract has no credit query yet, so the
        operator clears the fault from the dashboard after checking the machine.
        """
        return None

    def snapshot(self, state: str | None = None) -> SessionSnapshot:
        """Build a `SessionSnapshot` of the VMC's current live state.

        Public, read-only wrapper over `_snapshot` (VMC public surface
        design, section 3, Task 2): added because a test needs to capture
        a genuine mid-dispense snapshot and persist it by hand (to boot a
        second VMC against it and simulate a crash) with no event loop
        attached anywhere in the test file -- the normal production path
        (`_persist_then_dispense`/`_persist_session`) is fire-and-forget on
        the attached loop and cannot run there. Pure and side-effect free
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
            # _publish_status() snapshot is taken (on_vend_failed fully
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

    def _persist_session(self, state: str | None = None) -> None:
        """Save the live session, or remove the file once nothing is in flight."""
        if self._session_store is None:
            return
        if FaultCode.PAY_104 in self._faults.machine_faults:
            return  # keep the evidence file untouched until the operator clears it
        snap = self._snapshot(state)
        if snap.is_open():
            self._fire_and_forget(self._session_store.save_async(snap), persistent=True)
        else:
            self._fire_and_forget(self._session_store.clear_async(), persistent=True)

    def _update_display(self, target_state: str | None = None):
        """Update the customer-facing display based on the target FSM state.

        Pass *target_state* explicitly from ``before`` callbacks (the
        ``transitions`` library runs ``before`` hooks while ``self.state``
        still holds the *source* state).
        """
        if self._display_controller:
            self._display_controller.update_for_state(target_state or self.state)

    def _publish_status(self):
        """Publish current VMC status to MQTT (fire-and-forget).

        State updates to the health monitor and availability happen
        regardless of whether an MQTT client is attached; only the MQTT
        publish itself needs one.
        """
        if self._health_monitor:
            self._health_monitor.update_vmc_state(self.state)
        if self._availability:
            self._availability.set_fsm_state(self.state)
        self._persist_session()
        if self._mqtt_client is None or self._tasks.loop is None:
            return
        status = VMCStatus(
            state=self.state,
            credit_escrow=self.credit_escrow,
            selected_product=self.selected_product.name
            if self.selected_product
            else None,
            uptime_seconds=int(time.monotonic() - self._start_time),
        )
        self._fire_and_forget(self._mqtt_client.publish("status", status, retain=True))

    # --- Fault registry ---
    #
    # State and pure bookkeeping live in `self._faults`
    # (controller/fault_registry.py's `FaultRegistry`); the methods below
    # are thin delegates that keep every side effect (event recorder,
    # health monitor, MQTT, maintenance lease / session store guards)
    # exactly where it always was.

    @property
    def faults(self) -> FaultRegistry:
        """Read-only view of the fault registry (VMC public surface
        design, section 3). `vmc.faults.lockouts`/`.is_locked(sku)`/
        `.has(code)` are read directly; mutation only ever happens through
        `raise_fault`/`clear_fault`."""
        return self._faults

    # --- Escrow ledger ---
    #
    # State and pure bookkeeping live in `self._escrow`
    # (controller/escrow_ledger.py's `EscrowLedger`); the properties below
    # are read/write aliases kept because many existing tests read and
    # write `credit_escrow`/`escrow_credits` directly. The setters only
    # ever replace `total`/`credits` on the ledger -- a direct
    # `vmc.credit_escrow = x` assignment still cannot touch `credits`,
    # which is what lets the divergence guard in `_consume_credits_fifo`
    # keep working exactly as before this extraction.

    @property
    def credit_escrow(self) -> float:
        return self._escrow.total

    @credit_escrow.setter
    def credit_escrow(self, value: float) -> None:
        self._escrow.total = value

    @property
    def escrow_credits(self) -> list[Credit]:
        return self._escrow.credits

    @escrow_credits.setter
    def escrow_credits(self, value: list[Credit]) -> None:
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

    @property
    def gate(self) -> DispenserProfileGate:
        """Read-only view of the dispenser-profile gate (VMC public
        surface design, section 3). `vmc.gate.profiles` is read directly;
        mutation only ever happens through `set_dispenser_profiles`/
        `reconcile_dispenser_profiles`/`catalog_changed`."""
        return self._gate

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
        """Snapshot for the dashboard/health monitor. Product faults first."""
        return self._faults.snapshot()

    def _push_active_faults(self) -> None:
        faults = self.active_faults()
        if self._health_monitor:
            self._health_monitor.set_active_faults(faults)
        if self._availability:
            self._availability.set_active_faults(faults)

    def raise_fault(
        self,
        code: FaultCode,
        *,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record a fault: lock the product if its severity says so, alert the owner.

        Public entry point for both an in-FSM caller and a caller outside
        the FSM (e.g. main.py at startup, raising a machine-scope DATA-101/
        DATA-102) -- the two used to be split between this method (then
        private, ``_raise_fault``) and ``raise_data_fault``; they are merged
        here since neither ever did anything the other couldn't.
        """
        raised = self._faults.raise_fault(code, sku=sku, outcome=outcome)
        if raised.newly_locked and self._event_recorder:
            self._event_recorder.record(
                "lockout_set", metadata={"code": code.value, "sku": sku}
            )
        logger.error(f"FAULT {raised.message}")

        if self._health_monitor:
            self._fire_and_forget(
                self._health_monitor.raise_alert(
                    raised.alert_key,
                    raised.level,
                    "vmc",
                    raised.message,
                    code=code.value,
                    product_sku=sku,
                )
            )
        if self._mqtt_client:
            self._fire_and_forget(
                self._mqtt_client.publish(
                    "alerts",
                    VMCAlert(
                        level=raised.level,
                        message=raised.message,
                        code=code,
                        product_sku=sku,
                    ),
                )
            )
        self._push_active_faults()

    def _raise_fault(
        self,
        code: FaultCode,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        # deprecated: removed in the public-surface cleanup
        self.raise_fault(code, sku=sku, outcome=outcome)

    def raise_data_fault(self, code: FaultCode, outcome: str | None = None) -> None:
        # deprecated: removed in the public-surface cleanup
        self.raise_fault(code, outcome=outcome)

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key (SKU for product faults, code string for machine faults)."""
        code = self._faults.pop_lockout(key)
        if code is not None:
            sku = key
            if self._event_recorder:
                self._event_recorder.record(
                    "lockout_cleared",
                    metadata={"code": code.value, "sku": sku, "by": by},
                )
            if self._health_monitor:
                self._health_monitor.clear_alert(f"{code.value}:{sku}")
            logger.info(f"Fault {code.value} cleared for product {sku} ({by})")
            # Dispenser profiles (plan 2, Task 2 review fix): CFG-101 is a
            # standing invariant -- a product with no valid dispenser
            # profile is never sellable. Popping *any* lockout here (not
            # just CFG-101 itself, e.g. an operator clearing ICE-301 on a
            # profile-less product) can leave such a product unlocked with
            # no profile, since nothing else re-runs reconciliation on
            # this path. Re-check immediately and re-raise CFG-101 if no
            # valid profile exists. reconcile_dispenser_profiles's own
            # CFG-101 clears are unaffected: they only clear CFG-101 when
            # dispenser_profile_for already found a valid profile, so this
            # re-check finds one too and does nothing. No recursion is
            # possible: raise_fault never calls clear_fault.
            if self._gate.lacks_valid_profile(sku):
                logger.info(f"{sku} re-locked: no valid dispenser profile (CFG-101)")
                self.raise_fault(FaultCode.CFG_101, sku=sku)
        else:
            code = self._faults.parse_key(key)
            if code is None:
                return False
            if not self._faults.has(code):
                return False
            if code is FaultCode.SVC_102 and self._lease.hold is not None:
                # Copilot review (PR 22): a generic clear must not bypass
                # the maintenance lease invariant, the same class of bug
                # fixed twice already for PAY-104 in part 3. Only lease
                # release (end_maintenance / idle timeout / the last
                # in-flight run settling, all via `self._lease.release`)
                # may clear SVC-102; by the time that path calls
                # clear_fault it has already set self._lease.hold = None,
                # so this check cannot block the real release.
                logger.warning(
                    "Refused generic clear of SVC-102: maintenance lease "
                    f"still held by {self._lease.hold.holder_user_id}"
                )
                return False
            if code is FaultCode.PAY_104 and self._session_store:
                if not self._session_store.clear():
                    logger.error(
                        f"Fault {code.value}: could not remove session evidence file; "
                        "leaving fault in place."
                    )
                    return False
            self._faults.clear_machine(code)
            if code is FaultCode.PAY_104:
                if self._availability:
                    self._availability.set_transaction_certain(True)
            if self._health_monitor:
                self._health_monitor.clear_alert(f"{code.value}:machine")
            logger.info(f"Machine fault {code.value} cleared ({by})")
        self._push_active_faults()
        self._publish_status()
        return True

    def pending_sale_for_recovery(self) -> dict | None:
        """Read-only PAY-104 recovery accessor. See
        ``controller.session_recovery.SessionRecovery.pending_sale_for_recovery``.
        """
        return self._recovery.pending_sale_for_recovery()

    def pending_sale_already_recorded(self, pending: dict) -> bool:
        """In-memory record-once guard. See
        ``controller.session_recovery.SessionRecovery.pending_sale_already_recorded``.
        """
        return self._recovery.pending_sale_already_recorded(pending)

    def reserve_pending_sale(self, pending: dict) -> None:
        """In-memory record-once guard. See
        ``controller.session_recovery.SessionRecovery.reserve_pending_sale``.
        """
        self._recovery.reserve_pending_sale(pending)

    def mark_pending_sale_recorded(self) -> bool:
        """Durable record-once marker. See
        ``controller.session_recovery.SessionRecovery.mark_pending_sale_recorded``.
        """
        return self._recovery.mark_pending_sale_recorded()

    def _clear_ice101_lockouts(self) -> None:
        """Clear every ICE-101 lockout -- invoked by the telemetry router
        (controller/mqtt_inbound.py's `TelemetryRouter.handle_hardware_io`)
        when the vending ESP32 reports `bin_half_full` going true. Kept on
        the VMC because it drives the fault registry, not just telemetry.
        """
        for sku, code in list(self._faults.lockouts.items()):
            if code is FaultCode.ICE_101:
                self.clear_fault(sku, by="auto")

    async def on_hardware_io(self, topic: str, data: dict):
        """Binary hardware IO from the vending ESP32; ice returning clears ICE-101."""
        return await self._telemetry.handle_hardware_io(topic, data)

    async def _handle_mqtt_hardware_io(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_hardware_io(topic, data)

    # --- MQTT inbound handlers ---

    async def on_payment_credit(self, topic: str, data: dict):
        """Handle payment credit from MDB ESP32."""
        event = PaymentEvent.model_validate(data)
        logger.info(f"MQTT payment received: ${event.amount:.2f} via {event.method}")
        txn_log.info(f"PAYMENT RECEIVED: ${event.amount:.2f} via {event.method}")
        self.deposit_funds(event.amount, payment_method=event.method)

    async def _handle_mqtt_payment(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_payment_credit(topic, data)

    async def on_payment_status(self, topic: str, data: dict):
        """MDB device readiness; any device in error/offline blocks payment."""
        return await self._telemetry.handle_payment_status(topic, data)

    async def _handle_mqtt_payment_status(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_payment_status(topic, data)

    async def on_button_press(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        logger.info(f"MQTT button press: button {press.button}")
        txn_log.info(f"BUTTON PRESS: button {press.button}")
        vend_log.info(f"BUTTON PRESS: button {press.button}")
        self.select_product(press.button)

    async def _handle_mqtt_button(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_button_press(topic, data)

    def _dispenser_event_slot_mismatch(self, data: dict) -> bool:
        """True if `data`'s reported slot doesn't match the active sale's slot.

        A delayed/duplicate dispenser event (QoS 0, no dedup) for a slot other
        than the one currently being dispensed must not finalize or fault the
        wrong sale. No mismatch is reported when there's no active selection
        or the event carries no slot (nothing to compare against).
        """
        if self.selected_product is None:
            return False
        reported_slot = data.get("slot")
        if reported_slot is None:
            return False
        return reported_slot != self.selected_product.slot

    async def _record_sale(self) -> None:
        """Durably record the just-completed sale before `_finish_dispensing`.

        Runs the recorder's synchronous, own-connection ``record_sale``
        (services/event_recorder.py) off the event loop via
        ``asyncio.to_thread``, so the loop is never blocked on the disk
        write — the row is on disk before the FSM returns to idle (spec
        §1.2). ``record_sale`` already appends the same record to the sales
        journal (append + fsync) and re-raises on any insert failure; this
        only needs to catch that, raise the alert-class ``DATA-101``, and
        let the vend finish regardless — a storage problem must never fail
        the vend or stop the machine. It must not journal the record itself
        on top of that: ``record_sale`` already did.

        ``pending_sale_shares`` is cleared here on the success path (DB
        insert succeeded) and on the ordinary failure path (DB insert
        failed but the journal fallback caught it — ``DATA-101``).
        ``on_vend_failed`` already clears it (and restores the exact shares
        as credits) on a *dispense* failure path — clearing it here too is
        what stops the session snapshot from ever advertising an already-
        recorded sale as still pending; leaving it set would let a later
        "record this sale" PAY-104 recovery (Task 14) write the same money
        a second time.

        If the DB insert *and* the journal fallback both fail
        (``SaleRecordingFailed`` — e.g. a full or read-only data volume),
        the sale is recorded nowhere durable at all, so this deliberately
        does **not** clear ``pending_sale_shares`` and raises ``PAY-104``
        instead of ``DATA-101``. ``PAY-104`` is the fault
        ``pending_sale_for_recovery()`` already keys its Health › Faults
        "record this sale" / "discard" recovery on, and ``_persist_session``
        already refuses to touch the on-disk snapshot once ``PAY-104`` is
        active — so the "dispensing" snapshot already written at deduction
        time (``process_payment``) survives untouched as the sale's only
        remaining record, in this same running process, with no reboot
        required. Reusing this existing recovery path (rather than
        inventing a second one) is deliberate: it is exactly the situation
        that path already exists to handle — a sale whose completion is
        uncertain and must be reconciled by an operator.
        """
        product = self.selected_product
        if self._event_recorder is None or product is None:
            if self._sale is not None:
                self._sale = self._sale.with_(shares=None)
            return
        methods = self.pending_sale_shares or {"unknown": round(product.price, 2)}
        # Price comes from the consumed shares, not `product.price`:
        # `selected_product` is the *live* catalog object
        # (services/config_store.update_product mutates it in place), so an
        # operator can edit the price while this sale is mid-dispense. The
        # shares are what was actually deducted from escrow and must be
        # what gets recorded — matches the convention already used by
        # `pending_sale_for_recovery` for the PAY-104 recovery path.
        price = round(sum(methods.values()), 2)
        try:
            await asyncio.to_thread(
                self._event_recorder.record_sale,
                product.sku,
                product.name,
                product.slot,
                price,
                methods,
            )
        except SaleRecordingFailed:
            logger.exception(
                f"record_sale AND its journal fallback both failed for "
                f"sku={product.sku!r}; the sale is recorded nowhere durable. "
                "Raising PAY-104 and preserving pending_sale_shares so the "
                "on-disk session snapshot is not cleared — it is the only "
                "remaining record of this sale."
            )
            if self._availability:
                self._availability.set_transaction_certain(False)
            self.raise_fault(
                FaultCode.PAY_104,
                outcome=f"sku={product.sku} price=${price:.2f} unrecorded",
            )
            return  # do not clear pending_sale_shares — see docstring
        except Exception:
            logger.exception(
                f"record_sale failed for sku={product.sku!r}; already journaled "
                "as fallback by record_sale itself — raising DATA-101 and "
                "finishing the vend regardless"
            )
            self.raise_fault(
                FaultCode.DATA_101,
                outcome=f"sku={product.sku} price=${price:.2f}",
            )
        if self._sale is not None:
            self._sale = self._sale.with_(shares=None)

    async def on_dispenser_event(self, topic: str, data: dict):
        """Handle dispenser status from ESP32.

        Only DispenserOutcome members end a sale; every other `state` string
        is an intermediate hardware step and is logged.
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
        if self._dispenser_event_slot_mismatch(data):
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' for mismatched slot "
                f"{slot} (active sale is slot {self.selected_product.slot})"
            )
            return

        reported_request_id = data.get("request_id")
        in_flight_request_id = self._sale.request_id if self._sale else None
        if (
            reported_request_id
            and in_flight_request_id
            and reported_request_id != in_flight_request_id
        ):
            # Review finding C2 (Copilot, PR #32): a mismatched id is a
            # stale/foreign report -- e.g. a late report from an earlier
            # sale on the same slot -- and must be ignored outright, not
            # merely logged and processed anyway. A report carrying no
            # request_id at all (a pre-1.0.0 board) skips this check
            # entirely and is still accepted, keyed on slot and FSM state.
            logger.warning(
                f"Ignoring dispenser report: request_id={reported_request_id!r} "
                f"does not match in-flight request_id={in_flight_request_id!r} "
                f"(slot {slot})"
            )
            return

        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        # Review finding C3 (Copilot, PR #32): door_open is a customer
        # success only for a mechanism that actually has a
        # (mechanism, door_open) mapping in OUTCOME_FAULTS -- bagged_ice,
        # which maps it to ICE-402 (the bag released but the trap door
        # never closed). A mechanism with no such mapping (water_fill)
        # must take the ordinary failed-vend path below instead, via the
        # unmapped-outcome fallback in the `except KeyError` branch
        # further down -- never record a sale or raise ICE-402 for it.
        mechanism = self._sale.mechanism if self._sale else None
        door_open_is_success = (
            outcome is DispenserOutcome.door_open
            and _has_outcome_mapping(mechanism, DispenserOutcome.door_open)
        )
        if outcome is DispenserOutcome.complete or door_open_is_success:
            txn_log.info(f"DISPENSE SUCCESS: slot {slot}, product '{product_name}'")
            vend_log.info(f"DISPENSE COMPLETE: slot {slot}, product '{product_name}'")
            if self._sale is not None and self._sale.is_test:
                # is_test lives on the sale (self._sale.is_test, set only
                # by run_test_sale), not on the lease -- consulted here
                # instead of self._lease.hold so a lease release or
                # idle-timeout mid-run cannot flip this sale to production
                # (system-tests design §2.3). Neither a `sale` row nor a
                # `dispense` event is written; run_test_sale itself writes
                # the `test_run` event once it observes this outcome.
                # record_sale's own bookkeeping (clearing
                # pending_sale_shares) is replicated here since
                # _record_sale is skipped entirely for a test sale.
                self._sale = self._sale.with_(shares=None)
                self._resolve_test_sale_waiter("dispensed", None)
            else:
                if self._event_recorder and self.selected_product:
                    self._event_recorder.record(
                        "dispense", value=float(self.selected_product.slot)
                    )
                await self._record_sale()
            if outcome is DispenserOutcome.door_open:
                # door_open is a customer success (the ice/water was
                # released) but a hardware fault in its own right -- the
                # trap door failed to close, which is why ICE-402 is a
                # PAYMENT_BLOCKING_FAULTS member. Raised after recording the
                # sale (the customer did get their product) and before
                # _finish_dispensing, matching the "success path plus a
                # fault" shape the brief calls for.
                sku = self.selected_product.sku if self.selected_product else None
                self.raise_fault(FaultCode.ICE_402, sku=sku, outcome=outcome.value)
            self._finish_dispensing()
            return

        self._cancel_dispense_timeout()
        try:
            code = fault_for_outcome(mechanism, outcome)
        except KeyError:
            # A board mis-reporting for its own mechanism (e.g. a water
            # board sending `jam`, which only a bagged-ice slot can report)
            # -- fail safely with a generic error rather than let the
            # unmapped pair crash this MQTT handler. If the mechanism
            # itself is unknown (should not happen once on_dispense_product
            # always sets it), default to bagged_ice so this fallback
            # lookup can never itself KeyError.
            logger.error(
                f"No fault mapped for mechanism={mechanism!r} "
                f"outcome={outcome.value!r}; board may be mis-reporting for "
                "this mechanism -- falling back to a generic error"
            )
            code = fault_for_outcome(mechanism or "bagged_ice", DispenserOutcome.error)
        sku = self.selected_product.sku if self.selected_product else None
        txn_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        vend_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        self.raise_fault(code, sku=sku, outcome=outcome.value)
        # Captured BEFORE _fail_vend: it runs the vend_failed transition,
        # whose before-hook (on_vend_failed) fully clears self._sale (see
        # its own comment) before this call returns -- reading is_test
        # afterward would always see None/False and silently strand
        # run_test_sale's waiter forever.
        is_test = self._sale is not None and self._sale.is_test
        self._fail_vend(code, outcome=outcome.value)
        if is_test:
            self._resolve_test_sale_waiter("vend_failed", code.value)

    async def _handle_mqtt_dispenser(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_dispenser_event(topic, data)

    async def on_sensor_reading(self, topic: str, data: dict):
        """Handle temperature/sensor reading from ESP32."""
        return await self._telemetry.handle_sensor(topic, data)

    async def _handle_mqtt_sensor(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_sensor_reading(topic, data)

    async def on_water_flow(self, topic: str, data: dict):
        """Handle water flow sensor readings from the vending ESP32."""
        return await self._telemetry.handle_water_flow(topic, data)

    async def _handle_mqtt_water_flow(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_water_flow(topic, data)

    async def on_heartbeat(self, topic: str, data: dict):
        """Handle heartbeat from ESP32 subsystem."""
        return await self._telemetry.handle_heartbeat(topic, data)

    async def _handle_mqtt_heartbeat(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_heartbeat(topic, data)

    async def on_ice_maker_event(self, topic: str, data: dict):
        """Handle operational events from the ice maker ESP32."""
        return await self._telemetry.handle_ice_maker_event(topic, data)

    async def _handle_mqtt_ice_maker_event(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_ice_maker_event(topic, data)

    def _on_vending_capabilities_validated(
        self, subsystem: str, caps: SubsystemCapabilities
    ) -> None:
        """Re-run the dispenser-profiles capabilities cross-check whenever
        the vending board's retained capabilities doc validates -- invoked
        by the telemetry router (controller/mqtt_inbound.py's
        `TelemetryRouter.handle_capabilities`) only from its successful-
        validation branch.

        Dispenser profiles (plan: dispenser profiles, Task 2): the vending
        board's declared channel directions feed the profiles' own
        capabilities cross-check (drive channels must be outputs, sensors
        inputs) -- re-run it, and re-reconcile CFG-101, every time this doc
        changes. Only on a successfully validated doc: a malformed doc must
        never overwrite previously-good capabilities with something that
        would wrongly downgrade real slot errors back to warnings.
        """
        self._gate.on_vending_capabilities(subsystem, caps)

    async def on_capabilities(self, topic: str, data: dict):
        """Store a subsystem's retained self-description and hand it to health."""
        return await self._telemetry.handle_capabilities(topic, data)

    async def _handle_mqtt_capabilities(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_capabilities(topic, data)

    async def on_telemetry(self, topic: str, data: dict):
        """Route a generic telemetry channel reading into health tracking."""
        return await self._telemetry.handle_telemetry(topic, data)

    async def _handle_mqtt_telemetry(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_telemetry(topic, data)

    async def on_command_ack(self, topic: str, data: dict):
        """Log command acknowledgements from the monitor."""
        return await self._telemetry.handle_command_ack(topic, data)

    async def _handle_mqtt_command_ack(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_command_ack(topic, data)

    def _fire_and_forget(self, coro, *, persistent: bool = False) -> None:
        """Run a coroutine on the attached loop without awaiting it.

        Delegates to `self._tasks` (controller/task_runner.py) — see
        `TaskRunner.fire_and_forget` for the actual behavior.
        """
        self._tasks.fire_and_forget(coro, persistent=persistent)

    async def drain_persistence(self, timeout: float = 3.0) -> None:
        """Await in-flight session/inventory writes so shutdown never cancels them."""
        await self._tasks.drain_persistence(timeout=timeout)

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
    def set_qrcode_callback(self, callback):
        self.qrcode_callback = callback

    @logger.catch()
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        return self._escrow.has_credit

    @logger.catch()
    def set_update_callback(self, callback):
        self.update_callback = callback

    @logger.catch()
    def set_message_callback(self, callback):
        self.message_callback = callback

    @logger.catch()
    def send_customer_message(self, message):
        """Send a message to the customer via the registered callback."""
        logger.debug(f"Sending customer message: '{message}'")
        self._display_message(message)

    @logger.catch()
    def _refresh_ui(self):
        if self.update_callback:
            self.update_callback(self.state, self.selected_product, self.credit_escrow)

    @logger.catch()
    def _display_message(self, message):
        if self.message_callback:
            self.message_callback(message)

    # --- FSM Callback Methods ---
    def _after_state_change(self, *args, **kwargs):
        """Runs after every FSM transition with self.state already updated.

        Accepts and ignores *args/**kwargs — the ``transitions`` library
        forwards whatever arguments the trigger was called with (e.g.
        ``vend_failed(code=..., outcome=...)``) to every callback list,
        including ``after_state_change``.
        """
        self._publish_status()
        if self._test_sale_path is not None:
            self._test_sale_path.append(self.state)

    @logger.catch()
    def on_start_interaction(self):
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to interacting_with_user for product: {self.selected_product}"
        )
        self._reset_session_timeout()
        self._update_display("interacting_with_user")
        self._refresh_ui()
        self.send_customer_message(
            "Interaction started. Please insert funds or select a product."
        )

    @logger.catch()
    def on_dispense_product(self):
        logger.info(
            f"{STATE_CHANGE_PREFIX} Transitioning to dispensing for product: {self.selected_product}"
        )
        # Review finding I2: a fresh sequence number for this dispatch
        # attempt, captured by whichever path below ends up firing a
        # _fail_dispense_async for it (see _sale_seq's docstring in
        # __init__).
        self._sale_seq += 1
        seq = self._sale_seq
        self._cancel_session_timeout()
        self._update_display("dispensing")
        self._refresh_ui()
        self.send_customer_message(
            "Processing your payment and dispensing your product..."
        )
        # Tell the vending ESP32 which slot to dispense, through the command
        # dispatcher, carrying the slot's whole validated profile so a
        # dispensers.toml save mid-vend cannot affect this in-flight
        # command. Use the product's own stable `slot` field, NOT its
        # position in self.products — deleting an earlier product from the
        # catalog shifts list indices but must not change which physical
        # motor/slot a remaining product dispenses from.
        if self._tasks.loop and self.selected_product:
            product = self.selected_product
            profile = self.dispenser_profile_for(product)
            if profile is None:
                # Cannot happen after Task 2's CFG-101 lockout (select_product
                # already refuses a profile-less product) -- a defensive
                # fallback for the pathological case where the profile
                # vanished between selection and dispense (e.g. a concurrent
                # dispensers.toml reload racing the sale).
                #
                # on_dispense_product is the `before` callback for the
                # dispense_product transition (TRANSITIONS table above): the
                # FSM has not yet actually committed the move into
                # 'dispensing' while this callback is running, so firing the
                # vend_failed trigger synchronously here would be refused
                # ("Can't trigger event vend_failed from state
                # interacting_with_user!"). Deferred via _fire_and_forget so
                # it runs after this transition has actually completed.
                logger.error(
                    f"No valid dispenser profile for sku={product.sku!r} "
                    f"slot={product.slot} at dispense time; failing the vend"
                )
                self._fire_and_forget(
                    self._fail_dispense_async(FaultCode.CFG_101, "no_profile", seq),
                    persistent=True,
                )
                return
            cmd = DispenseCommand(
                slot=product.slot, mechanism=profile.mechanism, profile=profile
            )
            vend_log.info(
                f"DISPENSE CMD: slot {product.slot}, product '{product.name}', "
                f"mechanism {profile.mechanism}"
            )
            # Review finding C2 (Copilot, PR #32): the request_id must be
            # known *before* any terminal report can possibly arrive, not
            # learned only once the dispatcher's ack comes back -- a
            # report that wins the race against a slow ack would
            # otherwise be unverifiable. Generated and recorded here,
            # synchronously, before the dispatch task is even created.
            request_id = uuid4().hex
            self._sale = self._sale.with_(
                mechanism=profile.mechanism, request_id=request_id, seq=seq
            )
            snap = self._snapshot("dispensing") if self._session_store else None
            self._fire_and_forget(
                self._persist_then_dispense(snap, cmd, seq, request_id),
                persistent=True,
            )

    async def _fail_dispense_async(
        self, code: FaultCode, outcome: str, seq: int
    ) -> None:
        """Fail an in-flight dispense, but only if `seq` still names the
        *current* dispatch and the FSM is still in 'dispensing' when this
        actually runs.

        `seq` (review finding I2) guards against a late failure from an
        *earlier* sale's dispatch reaching here after that sale has
        already settled and a new sale has since reached 'dispensing':
        without this check, a delayed `CommandTimeout` for sale A (the
        dispatcher's own timeout window, not the 120s dispense-timeout
        fallback) could cancel sale B's dispense timer, raise PAY-102 on
        B's sku, and refund B's price while B's product is actually being
        dispensed. `seq` is compared against the live `self._sale_seq`
        (bumped once per `on_dispense_product` call), so only the dispatch
        that is still current may fail the vend.

        The state check below additionally covers the case where the
        *same* sale already settled through the real hardware report (a
        fire-and-forget dispatch task can race the terminal
        `hardware/dispenser` report), so a settled sale must never be
        double-failed.

        `on_dispense_product` schedules this via `_fire_and_forget` rather
        than awaiting it inline: it runs as the dispense_product
        transition's `before` callback, before the FSM has actually
        committed the move into 'dispensing', so firing the nested
        vend_failed trigger synchronously there would be refused by the
        FSM. `_persist_then_dispense` -- itself a separate task that only
        ever runs after that transition has committed -- awaits this
        directly instead; either call path is safe because of the checks
        below.
        """
        if seq != self._sale_seq:
            logger.warning(
                f"late dispatch failure for an earlier sale ignored "
                f"(code={code.value}, outcome={outcome!r}, seq={seq}, "
                f"current={self._sale_seq})"
            )
            return
        if self.state != "dispensing":
            logger.debug(
                "Dispense failed after the sale already left 'dispensing'; "
                "ignoring (not double-failing a settled sale)."
            )
            return
        self._cancel_dispense_timeout()
        sku = self.selected_product.sku if self.selected_product else None
        self.raise_fault(code, sku=sku, outcome=outcome)
        self._fail_vend(code, outcome=outcome)

    async def _persist_then_dispense(
        self, snap, cmd: DispenseCommand, seq: int, request_id: str
    ) -> None:
        """Write the dispensing snapshot to disk before the ESP32 is told to
        move, then send the dispense command through the command dispatcher
        and await only its accepted ack -- never completion.

        `request_id` (review finding C2) is `on_dispense_product`'s own
        generated id, already recorded on `self.sale.request_id`
        *before* this task was even created -- passed through to
        `send()` so the wire-level request_id the board sees is exactly
        the id the VMC can already match a terminal report against, no
        matter how the dispatch and the report race each other.

        A crash between the snapshot save and the dispatch leaves an open
        session on disk, so boot raises PAY-104 instead of forgetting that
        credit was taken and a vend was in flight.

        `seq` (review finding M4) is `on_dispense_product`'s own
        `self._sale_seq` snapshot, taken there and passed in rather than
        re-read from `self._sale_seq` here -- this task always names the
        dispatch it is actually performing, with no dependence on exactly
        when the event loop gets around to starting it relative to a later
        sale bumping the live counter.

        Review finding I1: everything from the dispatcher-wiring check
        onward (the "no dispatcher" branch, `send()` itself, and the
        ack-status check) is wrapped in one try/except, separate from the
        snapshot save above it. Originally only `send()`'s `CommandTimeout`
        was guarded; any other exception raised while dispatching -- bad
        `SubsystemCommand`/`model_dump` validation, a broken MQTT publish,
        anything -- escaped this task entirely, landed in
        `TaskRunner._log_task_failure`, and left the FSM stuck in 'dispensing' with
        the price already deducted until the full 120s dispense-timeout
        fallback. Every failure path here now fails the vend immediately
        instead, exactly like a `CommandTimeout` does.
        """
        try:
            if snap is not None and self._session_store is not None:
                if FaultCode.PAY_104 not in self._faults.machine_faults:
                    await self._session_store.save_async(snap)
        except Exception as exc:
            # Review finding M5: a snapshot-save failure gets its own
            # outcome string, distinct from a dispatch failure's "no_ack" --
            # the fault code is the same PAY-102 either way.
            logger.exception(
                f"VMC: failed to save dispensing snapshot for slot {cmd.slot}: {exc}"
            )
            await self._fail_dispense_async(FaultCode.PAY_102, "snapshot_failed", seq)
            return

        try:
            if self._command_dispatcher is None:
                logger.error(
                    "VMC: no command dispatcher attached; cannot send "
                    "dispense command (wiring error)"
                )
                await self._fail_dispense_async(FaultCode.PAY_102, "no_ack", seq)
                return

            ack = await self._command_dispatcher.send(
                "vending",
                "dispense",
                cmd.model_dump(mode="json"),
                request_id=request_id,
            )

            if ack.status != "ok":
                logger.error(
                    f"VMC: dispense ack for slot {cmd.slot} status={ack.status!r} "
                    f"detail={ack.detail!r}"
                )
                await self._fail_dispense_async(FaultCode.PAY_102, "no_ack", seq)
                return
        except CommandTimeout:
            logger.error(
                f"VMC: dispatcher timed out sending dispense for slot {cmd.slot}"
            )
            await self._fail_dispense_async(FaultCode.PAY_102, "no_ack", seq)
            return
        except Exception as exc:
            logger.exception(
                f"VMC: unexpected error dispatching dispense for slot {cmd.slot}: {exc}"
            )
            await self._fail_dispense_async(FaultCode.PAY_102, "no_ack", seq)
            return

        if ack.request_id != request_id:
            # Should not happen -- the dispatcher echoes back exactly the
            # id it was given -- but a surprise here must never clobber
            # the id already recorded for this (or, worse, a later) sale.
            logger.warning(
                f"VMC: dispense ack request_id={ack.request_id!r} does not "
                f"match the sent request_id={request_id!r} for slot {cmd.slot}"
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
        self._update_display(dest)
        self._refresh_ui()
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
        self.selected_product = None
        self.last_insufficient_message = ""
        self._update_display("idle")
        self._refresh_ui()

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
        self._cancel_dispense_timeout()
        self.request_refund(reason="cancel")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._update_display("idle")
        self._refresh_ui()
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
        per-method shares _consume_credits_fifo consumed for this sale
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
        self._cancel_dispense_timeout()
        shares = self.pending_sale_shares
        if self._sale is not None:
            self._sale = self._sale.with_(shares=None)
        if shares is None:
            # Should be unreachable: on_vend_failed only runs from
            # dispensing, which is only entered right after
            # _consume_credits_fifo sets pending_sale_shares. Guard, not a
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
        if self._event_recorder and not (self._sale is not None and self._sale.is_test):
            # Copilot review (PR 22, id=4128088653): on_vend_failed is the
            # one place that runs for every failed/timed-out vend,
            # production or test (both on_dispenser_event's failure
            # branch and _dispense_timed_out reach it through _fail_vend ->
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
            self._event_recorder.record(
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
            self._publish_status()
            self._update_display("idle")
        else:
            self.send_customer_message("Please choose another product.")
            self._reset_session_timeout()
            self._publish_status()
            self._update_display("interacting_with_user")
        self._refresh_ui()

    @logger.catch()
    def _dispense_timed_out(self):
        """No terminal dispenser report arrived within the configured timeout."""
        self._dispense_timeout_task = None
        if self.state != "dispensing":
            return
        sku = self.selected_product.sku if self.selected_product else None
        logger.error(
            f"Dispense timed out after {self.dispense_timeout_seconds:.0f}s with no "
            f"terminal report (slot {self.selected_product.slot if self.selected_product else '?'})"
        )
        self.raise_fault(FaultCode.PAY_102, sku=sku, outcome="no_report")
        # Captured BEFORE _fail_vend, which clears self._sale via
        # on_vend_failed before returning -- see _fail_vend's own comment.
        is_test = self._sale is not None and self._sale.is_test
        self._fail_vend(FaultCode.PAY_102, outcome="no_report")
        if is_test:
            # Kept as its own outcome ("timeout"), distinct from
            # "vend_failed", even though it runs through the same
            # vend_failed/PAY-102 transition above (system-tests design
            # §2.3; see TestSaleResult's docstring).
            self._resolve_test_sale_waiter("timeout", None)

    def _resolve_test_sale_waiter(self, outcome: str, fault_code: str | None) -> None:
        """Wake ``run_test_sale``'s waiter, if one is pending, with this
        outcome. A no-op if no test sale is in flight (waiter is ``None``)
        or it has already been resolved."""
        waiter = self._test_sale_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result((outcome, fault_code))

    @logger.catch()
    def on_error(self):
        logger.error(
            f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state."
        )
        if self._event_recorder:
            self._event_recorder.record("error", value=1.0)
        # Minor fix M3: see on_reset's comment above -- same reasoning.
        # Unlike on_reset, on_error does NOT clear selected_product (the
        # log line above still prints it), so this is a genuine partial
        # clear via with_ rather than the full self._sale = None on_reset
        # uses -- guarded because error_occurred can fire from any state,
        # including idle with no sale in progress at all.
        if self._sale is not None:
            self._sale = self._sale.with_(mechanism=None, request_id=None)
        # Pay out any remaining credit through the gateway
        had_credit = self.credit_escrow > 0
        if had_credit:
            self.request_refund(reason="error")
        self._update_display("error")
        self._refresh_ui()
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
        if self._lease.hold is not None and payment_method != "test":
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
        if self._availability and not self._availability.payment_enabled:
            logger.warning(
                f"Credit ${amount:.2f} arrived while payment is disabled "
                f"({', '.join(self._availability.payment_blocking_reasons())}); "
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
        self._publish_status()
        self._refresh_ui()
        self.send_customer_message(
            f"${amount:.2f} deposited. Current balance: ${self.credit_escrow:.2f}."
        )

    def _consume_credits_fifo(self, price: float) -> dict[str, float]:
        """Thin delegate to EscrowLedger.consume_fifo, kept so process_payment
        and its docstrings read as before. See controller/escrow_ledger.py
        for the FIFO-consumption and divergence-guard logic."""
        return self._escrow.consume_fifo(price)

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
        self._refresh_ui()

    def _publish_refund_command(self, cmd: PaymentRefundCommand) -> None:
        """Publish closure handed to RefundProtocol. Reads `_mqtt_client` at
        call time, not construction time -- it is still None when the VMC
        is built and only set later by `set_mqtt_client`."""
        if self._mqtt_client is not None:
            self._fire_and_forget(self._mqtt_client.publish("cmd/payment/refund", cmd))
        else:
            logger.warning("No MQTT client; refund command not sent")

    async def on_refund_ack(self, topic: str, data: dict):
        """Payment gateway acknowledged (or refused) a refund command."""
        result = PaymentRefundResult.model_validate(data)
        self._refunds.handle_ack(result)

    async def _handle_mqtt_refund_ack(self, topic: str, data: dict):
        # deprecated: removed in the public-surface cleanup
        return await self.on_refund_ack(topic, data)

    def _refund_confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._persist_session()
        txn_log.info(
            f"REFUND CONFIRMED: ${amount_returned:.2f} request_id={pending.request_id}"
        )
        if self._event_recorder:
            self._event_recorder.record(
                "refund",
                value=amount_returned,
                metadata={"request_id": pending.request_id, "reason": pending.reason},
            )
        self.send_customer_message(
            f"Refund of ${amount_returned:.2f} issued via {self.last_payment_method}."
        )

    def _refund_failed(self, pending: PendingRefund, detail: str) -> None:
        self._persist_session()
        txn_log.error(
            f"REFUND FAILED: ${pending.amount:.2f} request_id={pending.request_id} "
            f"reason={pending.reason} detail={detail}"
        )
        if self._event_recorder:
            self._event_recorder.record(
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
    # The lease lifecycle itself (the hold, its idle timer, the standby
    # sweep, and grant/release/takeover/run accounting) lives in
    # controller/maintenance_lease.py's MaintenanceLease, constructed as
    # self._lease in __init__. What stays here is the FSM/escrow
    # preconditions (begin_maintenance/begin_standby), the refunds,
    # run_test_sale itself, and the SVC-102 raise/clear wired to the lease
    # as on_granted/on_released callbacks. `lease` below is the VMC public
    # surface design's read-only collaborator access (section 3); tests
    # read `vmc.lease.hold`/`.idle_task`/`.sweep_task` directly and mutate
    # only through a VMC method (`begin_maintenance`, `end_maintenance`,
    # `take_over_maintenance`, ...) or, where the test is deliberately
    # exercising the lease itself rather than bypassing the VMC, through a
    # method on `vmc.lease` directly (e.g. `vmc.lease.release(...)`).
    # `maintenance_hold` is kept as its own convenience property since
    # routes (`web_interface/context.py`) read it by that name.

    @property
    def maintenance_hold(self) -> MaintenanceHold | None:
        """Read-only view of the current lease, if any. Never persisted."""
        return self._lease.hold

    @property
    def lease(self) -> MaintenanceLease:
        """Read-only view of the maintenance lease collaborator."""
        return self._lease

    def _release_maintenance_hold(self, by: str) -> None:
        self._lease.release(by)

    def _arm_maintenance_idle_timer(self) -> None:
        self._lease.arm_idle_timer()

    def _maintenance_idle_expired(self) -> None:
        self._lease.idle_expired()

    def set_session_liveness(self, predicate: Callable[[str], bool] | None) -> None:
        """Wire (or clear) the predicate the standby sweep uses to check the
        holder's web session (system-tests design §2.2a).

        ``predicate(session_id) -> bool`` should apply the same rules
        ``AccessStore.resolve_session`` would, without refreshing the
        session's own activity -- ``AccessStore.session_is_live`` is the
        intended implementation, wired in main.py. With no predicate wired
        (the default), a standby lease falls back to the ordinary idle
        timer instead of the sweep -- see ``MaintenanceLease.arm_sweep``.
        """
        self._lease.set_session_liveness(predicate)

    def _arm_maintenance_sweep(self) -> None:
        self._lease.arm_sweep()

    def _maintenance_sweep_tick(self) -> None:
        self._lease.sweep_tick()

    def begin_maintenance(
        self, user_id: str, session_id: str
    ) -> tuple[bool, str | None]:
        """Grant the maintenance lease.

        Returns ``(granted, reason)``: ``reason`` is ``None`` when granted,
        and a short human-readable refusal ("machine is mid-sale", "held by
        <id>") otherwise -- so a caller (the Tests level route, added by a
        later task) can tell the operator why without re-deriving it from
        VMC state. Granting requires the FSM to be idle, escrow to be zero
        (a mid-sale credit is a refusal even while idle -- the sale just
        hasn't been selected/dispensed yet), and no lease already held.
        """
        if self.state != "idle":
            return False, "machine is mid-sale"
        if not self._escrow.is_empty_within_tolerance:
            return False, "credit is still on the machine"
        if self._lease.hold is not None:
            return False, f"held by {self._lease.hold.holder_user_id}"
        self._lease.grant(user_id, session_id, standby=False)
        return True, None

    def begin_standby(self, user_id: str, session_id: str) -> tuple[bool, str | None]:
        """Take the machine out of service, making it idle first if needed
        (system-tests design §2.2a).

        Unlike ``begin_maintenance``, standby does not require the machine
        to already be idle with zero escrow -- it gets there itself:
        whatever credit is on the machine is refunded (``reason=
        "maintenance"``) and any live customer session is cancelled before
        the lease is granted with ``standby=True`` (no idle timer; the
        session sweep is armed instead).

        Refused exactly like ``begin_maintenance``'s two busy cases --
        ``"vend finishing, tap again"`` while ``dispensing`` (a running
        motor is never aborted), and ``"held by <id>"`` when another
        session already holds the lease. If the caller's own session
        already holds an (opportunistic) lease it is upgraded to standby
        in place rather than replaced.
        """
        if self.state == "dispensing":
            return False, "vend finishing, tap again"
        hold = self._lease.hold
        if hold is not None and hold.holder_session_id != session_id:
            return False, f"held by {hold.holder_user_id}"
        if hold is not None:
            self._lease.upgrade_to_standby(user_id, session_id)
            return True, None

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

        self._lease.grant(user_id, session_id, standby=True)
        return True, None

    def end_maintenance(self, session_id: str) -> bool:
        """Release the lease for its holder's session only.

        Returns False when there is no lease, or ``session_id`` is not its
        holder (refused either way). Returns True whenever the request is
        accepted -- either released immediately (``runs_in_flight == 0``),
        or deferred via ``release_requested`` for the last in-flight run to
        perform (`MaintenanceLease.run_finished`).
        """
        return self._lease.request_release(session_id)

    def take_over_maintenance(
        self, user_id: str, session_id: str
    ) -> tuple[bool, str | None]:
        """Transfer an idle, run-free lease to a new holder.

        Permitted only when no run is in flight and the lease has been idle
        (since ``last_activity_at``) for at least
        ``MAINTENANCE_TAKEOVER_IDLE_SECONDS``; records who took it over by
        overwriting the hold's holder fields in place. A standby lease
        (§2.2a) stays standby across the takeover -- the sweep is re-armed
        for the new holder's session rather than the idle timer.
        """
        return self._lease.take_over(user_id, session_id)

    def _maintenance_run_started(self) -> None:
        self._lease.run_started()

    def _maintenance_run_finished(self) -> None:
        self._lease.run_finished()

    def maintenance_test_run(self):
        """Bracket one test run against the lease.

        Increments ``runs_in_flight`` and refreshes ``last_activity_at`` on
        entry; decrements on exit via ``finally`` regardless of success,
        failure, or a timeout raised through the body -- so a run that
        fails still frees the lease's run count. ``run_test_sale`` wraps
        its dispatcher call and dispense-completion wait in this.
        """
        return self._lease.test_run()

    def _find_product_by_sku(self, sku: str) -> tuple[int | None, object | None]:
        """Return ``(button_index, product)`` for `sku` in the live catalog,
        or ``(None, None)``. Looked up by identity match against
        ``self.products`` (not ``list.index``, which compares by value and
        could pick the wrong entry for two otherwise-identical products)."""
        for index, product in enumerate(self.products):
            if product.sku == sku:
                return index, product
        return None, None

    async def run_test_sale(
        self,
        sku: str,
        *,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> TestSaleResult:
        """Run one simulated sale through the real FSM without ever
        recording it as a production sale (system-tests design §2.3).

        ``user_id``/``user_name`` (Task 13b, keyword-only, both default
        ``None`` so every pre-13b caller -- including tests/test_vmc_flows.py's
        TestRunTestSale, which calls this with only ``sku`` -- keeps working
        unchanged) identify who started the run for the ``test_run`` log row
        below and for the returned ``TestSaleResult.run_id``'s eventual
        verdict. The web route (``web_interface/routes/tests_level.py``'s
        ``POST /tests/sale``) is the only production caller that supplies
        them, from the authenticated ``Principal``.

        Requires the maintenance lease: wrapping the whole run in
        ``maintenance_test_run()`` is what enforces this -- its
        ``_maintenance_run_started`` raises ``RuntimeError`` when no lease
        is held, which is this method's refusal path. That also increments
        ``runs_in_flight`` for the duration, which is what stops the lease
        from being released out from under this run (system-tests design
        §2.2/§2.3).

        ``is_test`` is set on the sale itself (``self._sale.is_test``) here,
        not derived from the lease, and is what ``on_dispenser_event``,
        ``_dispense_timed_out`` and ``_fail_vend`` consult to keep this run
        out of the production sales ledger and away from a real refund
        command -- so releasing or losing the lease mid-run cannot flip
        this sale to a production one. Task 10's rule that the lease
        cannot be released while a run is in flight is the belt to this
        braces.

        Deposits the product's price as one credit with method ``"test"``
        -- the only credit ``deposit_funds`` accepts during a lease, see
        its lease branch above -- then selects the product and lets the
        *normal* dispense path run unmodified: the real FSM transitions,
        the real dispatch of ``dispense`` through the command dispatcher,
        and the real dispense-completion / dispense-timeout handling.
        Awaits whichever of the three
        terminal outcomes settles the sale via a one-shot ``Future``
        (``self._test_sale_waiter``) that those call sites resolve.

        Whatever the outcome, escrow is cleared directly at the end --
        never through ``request_refund``, which would publish a real
        ``cmd/payment/refund``: test money is not real money and this is
        not a refund (system-tests design §2.3).
        """
        product_index, product = self._find_product_by_sku(sku)
        if product is None:
            raise ValueError(f"run_test_sale: unknown product sku {sku!r}")

        # Dispenser profiles (plan: dispenser profiles, Task 2): refuse a
        # test sale for a product with no valid profile before touching
        # anything else -- no deposit, no lease, no runs_in_flight. Gated
        # on profiles actually being wired, like every other dispenser-
        # profiles check here, so a VMC with none set (every pre-plan-2
        # test and fixture) behaves exactly as before.
        if (
            self._gate.profiles is not None
            and self.dispenser_profile_for(product) is None
        ):
            raise RuntimeError(
                f"{product.sku} has no valid dispenser profile (CFG-101); "
                "fix dispensers.toml"
            )

        # Minted once per call, up front, so both the test_run row below and
        # the returned TestSaleResult carry the SAME id -- one run_id per
        # simulated sale, generated here rather than by the caller.
        #
        # This id being unique per call does NOT make two concurrent
        # run_test_sale calls safe on its own: self._test_sale_waiter and
        # self._test_sale_path (just below) are single instance attributes,
        # so a second overlapping call would silently overwrite the first
        # call's waiter, stranding the first `await waiter` on a Future
        # nothing will ever resolve -- a leaked runs_in_flight that pins
        # the maintenance lease until process restart. A prior version of
        # this comment claimed run_id uniqueness ruled that out; it did
        # not. The `_test_sale_in_progress` guard immediately below is what
        # actually prevents it, by refusing a second call outright while
        # one is already running.
        run_id = uuid4().hex

        # A single machine can only ever be mid one sale anyway -- the FSM
        # itself is single-sale by construction -- so a second, concurrent
        # simulated sale (a double-submit, or two browser tabs on the same
        # session; web_interface/routes/tests_level.py's
        # `_acquire_lease_or_refusal` deliberately lets a second command
        # through for a session that already holds the lease) is refused
        # outright here rather than accommodated. There is no `await`
        # between the check and the set, so under asyncio's single-threaded
        # event loop this check-and-set is atomic -- no other coroutine can
        # run between them and slip past the guard. This MUST happen
        # before `maintenance_test_run()` is entered below: refusing here
        # touches neither the lease nor `runs_in_flight`, so a refused
        # second call can never leak the counter it exists to protect.
        # Cleared in the `finally` below on every exit path -- success,
        # a raised exception, or cancellation.
        if self._test_sale_in_progress:
            raise RuntimeError(
                "run_test_sale: a simulated sale is already in progress; "
                "wait for it to finish (or time out) before starting "
                "another"
            )
        self._test_sale_in_progress = True
        try:
            with self.maintenance_test_run():
                # Seed the SaleContext with is_test=True *before*
                # select_product runs -- select_product's own availability
                # check (the test_sale_sellable vs. product_sellable
                # branch) reads self.sale.is_test before the product is
                # technically "selected", so is_test cannot wait for
                # select_product's own assignment to create the context.
                # The selected_product setter's "same product -> keep the
                # existing context" rule (see its own comment) is what
                # lets select_product's `self.selected_product = candidate`
                # below leave this seeded context (and its is_test=True)
                # alone rather than replacing it.
                self._sale = SaleContext(
                    product=product, is_test=True, started_at=time.time()
                )
                self._test_sale_path = [self.state]
                loop = self._tasks.loop or asyncio.get_running_loop()
                waiter: asyncio.Future = loop.create_future()
                self._test_sale_waiter = waiter
                started_at = time.time()
                try:
                    self.deposit_funds(round(product.price, 2), payment_method="test")
                    self.select_product(product_index)
                    if (
                        self.selected_product is not product
                        or self.state != "interacting_with_user"
                    ):
                        raise RuntimeError(
                            f"run_test_sale: could not select {sku!r} for a "
                            f"test sale (locked out, unavailable, or sold "
                            f"out; state={self.state!r})"
                        )
                    outcome, fault_code = await waiter
                    # _fail_vend's "no sellable products" branch forces idle
                    # via machine.set_state(), which (like _expire_session's
                    # own use of it elsewhere) bypasses after_state_change --
                    # so the settled state is appended explicitly here rather
                    # than trusted to have already landed in the path via
                    # that callback alone.
                    if (
                        not self._test_sale_path
                        or self._test_sale_path[-1] != self.state
                    ):
                        self._test_sale_path.append(self.state)
                    path = list(self._test_sale_path)
                    if self._event_recorder is not None:
                        # The seam a later task's recorder-side work fills in
                        # (system-tests design §4): this already lands in
                        # the existing `events` table via the same generic
                        # `record()` every other event type uses (`dispense`,
                        # `vend_failed`, `refund`, ...); nothing in
                        # services/event_recorder.py needed changing for
                        # that.
                        #
                        # Task 13b extended this metadata dict (originally
                        # just sku/outcome/fault_code/path) with run_id/
                        # user_id/user_name/subsystem/command/params/status/
                        # checks/verdict/note so this row renders through
                        # the SAME tests_log.html branch and is reachable by
                        # POST /tests/runs/{run_id}/verdict -- the pre-13b
                        # shape had no run_id, so a simulated sale's row
                        # could never be verdicted at all (see
                        # TestSaleResult's docstring and this method's own
                        # docstring). This is the ONLY place a simulated
                        # sale writes a test_run row -- one row per call,
                        # never a second write from the route side
                        # (web_interface/routes/tests_level.py's
                        # POST /tests/sale reuses this result's `run_id`
                        # rather than writing its own row).
                        self._event_recorder.record(
                            "test_run",
                            value=round(time.time() - started_at, 3),
                            metadata={
                                "run_id": run_id,
                                "user_id": user_id,
                                "user_name": user_name,
                                "subsystem": None,
                                "command": "simulated_sale",
                                "params": {"sku": sku},
                                "status": "ok" if outcome == "dispensed" else "failed",
                                "checks": None,
                                "verdict": None,
                                "note": None,
                                "sku": sku,
                                "outcome": outcome,
                                "fault_code": fault_code,
                                "path": path,
                            },
                        )
                finally:
                    self._test_sale_path = None
                    self._test_sale_waiter = None
                    # Whenever the sale actually settled (dispensed, failed,
                    # or timed out), on_complete_transaction/on_vend_failed
                    # already fully cleared self._sale (selected_product =
                    # None) before `await waiter` ever returned -- so
                    # self.state is never still "dispensing" here on that
                    # path, and the `else` branch below is a no-op. The two
                    # cases where self.state IS still "dispensing" are: (a)
                    # this call's own task was cancelled while suspended on
                    # `await waiter`, mid-vend, with the real hardware
                    # dispense still physically in flight -- stripping only
                    # is_test (not the whole context) matches today's
                    # behaviour, where only the separate _sale_is_test flag
                    # was reset here, leaving selected_product/mechanism/
                    # request_id exactly as the stuck vend left them, so a
                    # later real hardware report can still settle it. The
                    # other case -- select_product refused the seeded
                    # context outright (locked out/unavailable/sold out) --
                    # never reaches "dispensing" at all, so it always takes
                    # the `else` branch, clearing the leftover seeded
                    # context so a refused test sale leaves
                    # vmc.selected_product is None, exactly as it did before
                    # this sale was ever seeded onto self._sale.
                    if self.state == "dispensing":
                        if self._sale is not None:
                            self._sale = self._sale.with_(is_test=False)
                    else:
                        self._sale = None
                    # Test money is never real money and must never leave
                    # via a refund command (system-tests design §2.3) --
                    # clear it directly rather than through request_refund.
                    # Whether the sale dispensed (escrow already at 0 -- the
                    # deposit was exactly the price) or failed/timed out
                    # (on_vend_failed restored the price to escrow), this is
                    # a no-op in the former case and the actual clear in the
                    # latter.
                    self._escrow.take_all()

            return TestSaleResult(
                sku=sku,
                path=path,
                outcome=outcome,
                fault_code=fault_code,
                run_id=run_id,
            )
        finally:
            # Reached on every exit from the guarded body above -- a clean
            # return, a raised exception (including the "could not select"
            # RuntimeError before `await waiter` is ever reached), or a
            # CancelledError from this call's own task being cancelled
            # while suspended in `await waiter`. Symmetric with the guard
            # set just above: the next call (from this session, once the
            # lease is still held, or a future one) sees a clean slate
            # regardless of how this one ended.
            self._test_sale_in_progress = False

    @logger.catch()
    def initiate_virtual_payment(self, amount):
        """
        Initiates a virtual payment by generating a payment URL and corresponding QR code.
        Cycles through available virtual payment gateways.
        """
        gateways = list(self.payment_gateway_manager.gateways.keys())
        logger.debug(f"Available virtual payment gateways: {gateways}")
        if not gateways:
            logger.error("No virtual payment gateways configured.")
            self.send_customer_message("Virtual payment is currently unavailable.")
            return

        current_gateway = gateways[self.virtual_payment_index]
        logger.info(
            f"Initiating virtual payment via {current_gateway} for amount ${amount:.2f}"
        )
        payment_url = self.payment_gateway_manager.gateways[
            current_gateway
        ].generate_payment_url(amount)
        logger.debug(f"Generated payment URL: {payment_url}")

        qr_image = self.payment_gateway_manager.generate_qr_code(
            current_gateway, amount
        )
        if self.qrcode_callback:
            self.qrcode_callback(qr_image)
        self.send_customer_message(
            f"Virtual Payment Option ({current_gateway}): Scan the QR code above."
        )
        self.virtual_payment_index = (self.virtual_payment_index + 1) % len(gateways)

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

        if self._availability:
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
                sellable, failing = self._availability.test_sale_sellable(candidate)
            else:
                sellable, failing = self._availability.product_sellable(candidate)
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

        if self._inventory and not self._inventory.is_available(
            self.selected_product.sku
        ):
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
        self._refresh_ui()

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
            self._sale = self._sale.with_(shares=self._consume_credits_fifo(price))
            self.credit_escrow -= price
            logger.debug(
                f"Deducted price from escrow. New escrow: {self.credit_escrow:.2f} "
                f"(shares: {self.pending_sale_shares})"
            )
            self.dispense_product()
            self._persist_session("dispensing")
            self._refresh_ui()
            # Dispenser hardware reports a terminal DispenserOutcome via MQTT; no
            # report within the timeout is a failed vend (PAY-102).
            self._dispense_timeout_task = self._schedule(
                self.dispense_timeout_seconds,
                self._dispense_timed_out,
                label="dispense_timeout",
            )
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

    def _process_payment(self):
        # deprecated: removed in the public-surface cleanup
        self.process_payment()

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
            self._publish_status()
            self._refresh_ui()
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
            self._publish_status()
            self._refresh_ui()
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
        self._publish_status()
        self._update_display("idle")
        self._refresh_ui()

    def _cancel_dispense_timeout(self):
        """Cancel the dispense fallback timeout if it is still pending."""
        if self._dispense_timeout_task and not self._dispense_timeout_task.done():
            self._dispense_timeout_task.cancel()
        self._dispense_timeout_task = None

    @logger.catch()
    def _finish_dispensing(self):
        logger.debug(
            f"Finishing dispensing process for product: {self.selected_product}"
        )
        self._cancel_dispense_timeout()
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
        if self._inventory and self.selected_product:
            sku = self.selected_product.sku
            if self._inventory.is_tracked(sku):
                self._inventory.decrement(sku, persist=False)
                self._fire_and_forget(self._inventory.save_async(), persistent=True)
                logger.info(
                    f"Inventory for {self.selected_product.name} updated: {self._inventory.get_count(sku)} remaining."
                )
        self.complete_transaction()
        self._persist_session()
        self._refresh_ui()
