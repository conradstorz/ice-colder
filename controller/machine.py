"""`Machine`: the composition root that builds the VMC and every collaborator
and owns the service wiring (vmc-reduction plan, Task 5).

Tasks 1-4 carved `StatusOutputs`, `FaultRegistry`/`FaultService`,
`EscrowLedger`, `RefundProtocol`, `SessionRecovery`, `TelemetryRouter`,
`MaintenanceLease` and `DispenserProfileGate` off the VMC god object one at a
time, with the VMC itself still constructing every one of them in its own
`__init__`. `Machine` is the next step: it builds all of those collaborators
*itself*, in the order below, then builds the `VMC` and hands six of them in
(`escrow`, `refunds`, `faults`, `outputs`, `gate`, `lease` -- all now
optional keyword-only `VMC.__init__` parameters) so the VMC stores and uses
the exact same objects rather than building its own. `recovery` and
`telemetry` are *not* passed into the VMC -- they stay only on `Machine`, so
the VMC's own internal copies of those two (kept, unchanged, until Task 6
deletes them) are a deliberate, temporary duplication during this
transition. `availability`/`inventory` are passed as zero-arg callables
(`lambda: self.availability`/`lambda: self.inventory`) rather than objects,
because the live `Availability`/`InventoryManager` instance is attached long
after construction, via `Machine.set_availability`/`set_inventory_manager`.

**Why some collaborators are built with `lambda *a: self.vmc.<method>(*a)`.**
A handful of closures a collaborator needs are irreducibly about the live
VMC's FSM state or its own private methods -- the FSM's current state
(`outputs.fsm_state`, `faults.fsm_state`), a full session snapshot
(`outputs.snapshot`, which reads `self.state`/`self.credit_escrow`/
`self.selected_product`/the in-flight sale), and the refund protocol's
terminal callbacks (`refunds.on_confirmed`/`on_failed`, which are VMC's own
`_refund_confirmed`/`_refund_failed` -- transaction-log lines, event-recorder
rows, and the customer-facing message) and the maintenance lease's grant/
release callbacks (`lease.on_granted`/`on_released`, which raise/clear
SVC-102 through the VMC's own `raise_fault`/`clear_fault`). `Machine` builds
every one of these collaborators *before* `self._vmc` exists (the `VMC(...)`
call is last), so each such closure is written as a `lambda` referencing
`self.vmc` rather than a bound method captured at construction time --
Python resolves `self.vmc` only when the lambda is actually *called*, by
which point `Machine.__init__` has long since finished and `self._vmc` is
set. No closure here is ever invoked during construction, so this ordering
is never actually circular, only apparently so.

Every other collaborator's closures route through `Machine`'s own already-
built state instead of the VMC (`self._faults`, `self._outputs.availability`,
`self._config.products`, `self._tasks.schedule`, ...), since those don't need
live FSM state at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from loguru import logger

from config.config_model import ConfigModel
from contracts.vending_machine import FaultCode
from controller import mqtt_inbound
from controller.dispenser_gate import DispenserProfileGate
from controller.escrow_ledger import EscrowLedger
from controller.fault_registry import FaultRegistry
from controller.fault_service import FaultService
from controller.maintenance_lease import MaintenanceHold, MaintenanceLease
from controller.outputs import StatusOutputs
from controller.refund_protocol import RefundProtocol
from controller.session_recovery import SessionRecovery
from controller.task_runner import TaskRunner
from controller.vmc import VMC
from services.availability import Availability
from services.dispensers import DispenserProfiles
from services.display_controller import DisplayController
from services.health_monitor import HealthMonitor
from services.inventory_manager import InventoryManager
from services.session_store import SessionSnapshot, SessionStore

# Bound logger for the transaction log -- mirrors controller/vmc.py's own
# `txn_log` / controller/mqtt_inbound.py's `ice_log`: loguru's bind() only
# attaches the `transaction` extra field, and sinks are resolved at log
# time, so binding here at import is safe even though setup_logging() runs
# later in main().
_txn_log = logger.bind(transaction=True)


class Machine:
    """Builds the VMC and every collaborator; owns the service wiring that
    used to live directly on `VMC.__init__` and its `set_*` methods. See the
    module docstring above for construction order and the `lambda
    *a: self.vmc.<method>(*a)` pattern.
    """

    def __init__(self, config: ConfigModel, *, tasks: TaskRunner | None = None) -> None:
        self._config = config
        self._tasks = tasks if tasks is not None else TaskRunner()
        self._escrow = EscrowLedger()
        self._registry = FaultRegistry(self._product_name)

        # Sinks attached later via their own set_* method; None until then,
        # exactly like the VMC attributes they replace. The health monitor
        # has no separate attribute -- `self._outputs.health` (set by
        # `set_health_monitor` below, via `attach_health`) is the single
        # source of truth, read by `health_monitor` and the telemetry
        # router's `health` closure alike.
        self._event_recorder = None
        self._command_dispatcher = None
        self._inventory: InventoryManager | None = None
        self._subsystem_capabilities: dict[str, dict] = {}

        self._outputs = StatusOutputs(
            snapshot=lambda state: self.vmc.snapshot(state),
            fsm_state=lambda: self.vmc.state,
            credit_escrow=lambda: self._escrow.total,
            selected_product=lambda: self.vmc.selected_product,
            pay104_active=lambda: self._faults.has(FaultCode.PAY_104),
            tasks=self._tasks,
        )
        self._faults = FaultService(
            registry=self._registry,
            outputs=self._outputs,
            tasks=self._tasks,
            recorder=lambda: self._event_recorder,
            lease_holder=lambda: (
                self._lease.hold.holder_user_id if self._lease.hold else None
            ),
            lacks_valid_profile=lambda sku: self._gate.lacks_valid_profile(sku),
            set_transaction_certain=lambda certain: (
                self._outputs.availability.set_transaction_certain(certain)
                if self._outputs.availability
                else None
            ),
            fsm_state=lambda: self.vmc.state,
        )
        self._refunds = RefundProtocol(
            publish=self._outputs.publish_refund,
            schedule=self._tasks.schedule,
            on_confirmed=lambda *a: self.vmc._refund_confirmed(*a),
            on_failed=lambda *a: self.vmc._refund_failed(*a),
            ack_timeout=lambda: self.vmc.REFUND_ACK_TIMEOUT,
            max_attempts=lambda: self.vmc.REFUND_MAX_ATTEMPTS,
        )
        self._gate = DispenserProfileGate(
            products=lambda: self._config.products,
            is_locked=self._faults.is_locked,
            has_machine_fault=self._faults.has,
            raise_fault=self._faults.raise_fault,
            clear_fault=lambda key, by: self._faults.clear_fault(key, by=by),
        )
        self._lease = MaintenanceLease(
            schedule=self._tasks.schedule,
            on_granted=lambda: self.vmc.raise_fault(
                FaultCode.SVC_102, outcome="maintenance_lease_granted"
            ),
            on_released=lambda by: self.vmc.clear_fault(FaultCode.SVC_102.value, by=by),
            idle_timeout=lambda: self.vmc.MAINTENANCE_IDLE_TIMEOUT_SECONDS,
            takeover_idle=lambda: self.vmc.MAINTENANCE_TAKEOVER_IDLE_SECONDS,
            sweep_seconds=lambda: self.vmc.STANDBY_SWEEP_SECONDS,
        )
        self._recovery = SessionRecovery(
            store=lambda: self._outputs.session_store,
            product_name=self._product_name,
            pay104_active=lambda: self._faults.has(FaultCode.PAY_104),
        )
        self._telemetry = mqtt_inbound.TelemetryRouter(
            health=lambda: self._outputs.health,
            availability=lambda: self._outputs.availability,
            capabilities=self._subsystem_capabilities,
            on_bin_half_full=self._faults.clear_ice101_lockouts,
            on_capabilities_validated=self._gate.on_vending_capabilities,
        )

        self._vmc = VMC(
            config,
            tasks=self._tasks,
            escrow=self._escrow,
            refunds=self._refunds,
            faults=self._faults,
            outputs=self._outputs,
            gate=self._gate,
            lease=self._lease,
            availability=lambda: self.availability,
            inventory=lambda: self.inventory,
            recorder=lambda: self.event_recorder,
            dispatcher=lambda: self.command_dispatcher,
        )

    def _product_name(self, sku: str | None) -> str | None:
        """Shared by the fault registry and session recovery -- mirrors
        `VMC._product_name` exactly, reading the catalog straight off the
        `ConfigModel` rather than through the VMC."""
        if sku is None:
            return None
        return next((p.name for p in self._config.products if p.sku == sku), sku)

    # --- read-only collaborator properties ---

    @property
    def vmc(self) -> VMC:
        return self._vmc

    @property
    def tasks(self) -> TaskRunner:
        return self._tasks

    @property
    def escrow(self) -> EscrowLedger:
        return self._escrow

    @property
    def faults(self) -> FaultService:
        return self._faults

    @property
    def outputs(self) -> StatusOutputs:
        return self._outputs

    @property
    def refunds(self) -> RefundProtocol:
        return self._refunds

    @property
    def gate(self) -> DispenserProfileGate:
        return self._gate

    @property
    def lease(self) -> MaintenanceLease:
        return self._lease

    @property
    def recovery(self) -> SessionRecovery:
        return self._recovery

    @property
    def telemetry(self) -> mqtt_inbound.TelemetryRouter:
        return self._telemetry

    @property
    def session_store(self) -> SessionStore | None:
        return self._outputs.session_store

    @property
    def mqtt_client(self):
        return self._outputs.mqtt

    @property
    def command_dispatcher(self):
        return self._command_dispatcher

    @property
    def health_monitor(self) -> HealthMonitor | None:
        return self._outputs.health

    @property
    def availability(self) -> Availability | None:
        return self._outputs.availability

    @property
    def event_recorder(self):
        return self._event_recorder

    @property
    def display_controller(self) -> DisplayController | None:
        return self._outputs.display_controller

    @property
    def inventory(self) -> InventoryManager | None:
        return self._inventory

    @property
    def maintenance_hold(self) -> MaintenanceHold | None:
        """Read-only view of the current lease, if any. Never persisted."""
        return self._lease.hold

    @property
    def subsystem_capabilities(self) -> dict[str, dict]:
        return self._subsystem_capabilities

    # --- wiring methods (moved verbatim from the VMC) ---

    def attach_to_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the running asyncio event loop. Must be called before scheduling."""
        self._tasks.attach(loop)
        logger.debug("Machine attached to asyncio event loop.")

    def cancel_pending_tasks(self) -> None:
        """Cancel all pending scheduled tasks. Call during shutdown.

        Persistence writes tracked by the task runner are never cancelled
        here -- they are drained (awaited to completion) by
        `drain_persistence()` instead, so a shutdown cannot truncate an
        in-flight session/inventory save.
        """
        self._tasks.cancel_pending()
        self._vmc.cancel_timers()
        self._refunds.cancel_all()
        logger.debug("Machine: all pending tasks cancelled.")

    async def drain_persistence(self, timeout: float = 3.0) -> None:
        """Await in-flight session/inventory writes so shutdown never cancels them."""
        await self._tasks.drain_persistence(timeout=timeout)

    def set_mqtt_client(self, client) -> None:
        """Attach an MQTTClient instance for publishing status and receiving events."""
        self._outputs.attach_mqtt(client)
        # SUBSCRIPTIONS (controller/mqtt_inbound.py) is the single source of
        # truth for which topics map to which handler, as (topic, owner,
        # method) triples -- owner "vmc" resolves against self.vmc, owner
        # "telemetry" against self._telemetry.
        for topic, owner, name in mqtt_inbound.SUBSCRIPTIONS:
            target = self._vmc if owner == "vmc" else self._telemetry
            client.register(topic, getattr(target, name))
        logger.debug("Machine registered MQTT handlers.")

    def set_health_monitor(self, monitor: HealthMonitor) -> None:
        """Attach a HealthMonitor; its liveness transitions become COM/PAY faults."""
        self._outputs.attach_health(monitor)
        monitor.set_liveness_callback(self._faults.on_subsystem_liveness)
        logger.debug("Machine attached health monitor.")

    def set_availability(self, availability: Availability) -> None:
        """Attach the permissive table; it publishes cmd/payment/enable through us."""
        self._outputs.attach_availability(availability)
        availability.set_fsm_state(self._vmc.state)
        availability.set_active_faults(self._faults.active_faults())
        availability.set_publisher(self._outputs.publish_payment_enable)
        logger.debug("Machine attached availability.")

    def set_display_controller(self, controller: DisplayController) -> None:
        """Attach a DisplayController so FSM state changes update the customer display."""
        self._outputs.attach_display(controller)
        logger.debug("Machine attached display controller.")

    def set_inventory_manager(self, inventory: InventoryManager) -> None:
        """Attach an InventoryManager for persistent stock tracking."""
        self._inventory = inventory
        logger.debug("Machine attached inventory manager.")

    def set_event_recorder(self, recorder) -> None:
        """Attach an EventRecorder so FSM error events are persisted."""
        self._event_recorder = recorder
        logger.debug("Machine attached event recorder.")

    def set_session_store(self, store: SessionStore) -> None:
        """Attach the session store and evaluate any snapshot left by a previous run.

        Call after attach_to_loop, set_health_monitor and set_availability so
        the PAY-104 alert and the availability gate both land.
        """
        self._outputs.attach_session_store(store)
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
        logger.debug("Machine attached session store.")

    def set_command_dispatcher(self, dispatcher) -> None:
        """Attach the subsystem CommandDispatcher (system-tests design §2.1)."""
        self._command_dispatcher = dispatcher
        logger.debug("Machine attached command dispatcher.")

    def set_dispenser_profiles(self, profiles: DispenserProfiles) -> None:
        """Attach the loaded `DispenserProfiles` -- delegates to
        `self._gate.attach` (controller/dispenser_gate.py)."""
        self._gate.attach(profiles)

    def set_session_liveness(self, predicate) -> None:
        """Wire (or clear) the predicate the standby sweep uses to check the
        holder's web session (system-tests design §2.2a) -- delegates to
        `self._lease.set_session_liveness`."""
        self._lease.set_session_liveness(predicate)

    def on_mqtt_connection(self, connected: bool) -> None:
        """Connection-state callback from MQTTClient (chained after the
        health monitor) -- delegates to `self._faults.on_mqtt_connection`."""
        self._faults.on_mqtt_connection(connected)

    def _flag_uncertain_session(self, snap: SessionSnapshot) -> None:
        """Side effects of an 'uncertain' boot decision other than
        `SessionStore.clear()` itself (which `SessionRecovery.
        evaluate_at_boot` already performs for the other two decision
        kinds) -- mirrors `VMC._flag_uncertain_session` exactly."""
        detail = snap.error or (
            f"state={snap.state} escrow=${snap.credit_escrow:.2f} "
            f"sku={snap.selected_sku} refund={snap.pending_refund_request_id}"
        )
        logger.error(f"Transaction uncertain after restart: {detail}")
        _txn_log.error(f"RESTART WITH OPEN SESSION: {detail}")
        if self._event_recorder:
            self._event_recorder.record(
                "session_uncertain", value=snap.credit_escrow, metadata=asdict(snap)
            )
        availability = self._outputs.availability
        if availability:
            availability.set_transaction_certain(False)
        self._faults.raise_fault(FaultCode.PAY_104, outcome=detail)

    # --- recovery conveniences (one-line forwards; routes call them in Task 6) ---

    def pending_sale_for_recovery(self) -> dict | None:
        return self._recovery.pending_sale_for_recovery()

    def pending_sale_already_recorded(self, pending: dict) -> bool:
        return self._recovery.pending_sale_already_recorded(pending)

    def reserve_pending_sale(self, pending: dict) -> None:
        self._recovery.reserve_pending_sale(pending)

    def mark_pending_sale_recorded(self) -> bool:
        return self._recovery.mark_pending_sale_recorded()
