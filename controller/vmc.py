# controller/vmc.py
import asyncio
import time
from dataclasses import asdict, dataclass
from uuid import uuid4
from transitions import Machine
from loguru import logger
from pydantic import ValidationError
from services.payment_gateway_manager import PaymentGatewayManager
from services.mqtt_messages import (
    VMCStatus,
    PaymentEvent,
    PaymentEnableCommand,
    PaymentStatus,
    ButtonPress,
    DispenseCommand,
    IceMakerEvent,
    HardwareIO,
    VMCAlert,
)
from contracts.ice_maker_monitor import ChannelReading, CommandAck
from contracts.vending_machine import (
    FAULT_TABLE,
    OUTCOME_FAULTS,
    DispenserOutcome,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
    Scope,
    Severity,
    SubsystemCapabilities,
)
from config.config_model import ConfigModel
from services.availability import Availability
from services.health_monitor import HealthMonitor
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from services.session_store import SessionSnapshot, SessionStore

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

# Bound loggers — initialized lazily so sinks are installed before first use.
# Module-level references are set by VMC.__init__() (after setup_logging() in main.py).
txn_log = logger
ice_log = logger
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

# Alert level sent to the owner for each fault severity.
_SEVERITY_LEVEL = {
    Severity.info: "info",
    Severity.warning: "warning",
    Severity.product_unavailable: "warning",
    Severity.vend_failed: "warning",
    Severity.lockout: "error",
    Severity.critical: "critical",
}

# Heartbeat loss per subsystem -> registry fault (ROADMAP §5, §8).
_LIVENESS_FAULTS = {
    "vending": FaultCode.COM_101,
    "ice_maker": FaultCode.COM_102,
    "mdb": FaultCode.PAY_101,
}


@dataclass
class PendingRefund:
    request_id: str
    amount: float
    reason: str
    attempts: int = 1
    deadline_task: asyncio.Task | None = None


class VMC:
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    REFUND_ACK_TIMEOUT = 10.0  # seconds to wait for cmd/payment/refund/ack
    REFUND_MAX_ATTEMPTS = 2  # one retry with the same request_id, then PAY-103

    @logger.catch()
    def __init__(self, config: ConfigModel):
        global txn_log, ice_log, vend_log
        txn_log = logger.bind(transaction=True)
        ice_log = logger.bind(ice_maker=True)
        vend_log = logger.bind(vending=True)
        logger.debug("Initializing VMC with pre-loaded ConfigModel")

        self.config_model = config
        logger.debug(self.config_model.model_dump_json(exclude_none=True, indent=2))

        self.products = self.config_model.products
        self.owner_contact = self.config_model.machine_owner

        self.selected_product = None
        self.credit_escrow = 0.0
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"

        self.update_callback = None
        self.message_callback = None
        self.qrcode_callback = None

        self._pending_tasks: list[asyncio.Task] = []
        self._persist_tasks: list[asyncio.Task] = []
        self._dispense_timeout_task: asyncio.Task | None = None
        self._session_timeout_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
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
        self.subsystem_capabilities: dict[str, dict] = {}
        # Fault registry: product-scope faults by SKU, machine-scope faults by code.
        self._lockouts: dict[str, FaultCode] = {}
        self._machine_faults: dict[FaultCode, float] = {}
        self._pending_refunds: dict[str, PendingRefund] = {}
        self._start_time = time.monotonic()
        self._session_timeout_seconds = 180.0  # 3 minutes
        self._dispense_timeout_seconds = (
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
        self._loop = loop
        logger.debug("VMC attached to asyncio event loop.")

    def cancel_pending_tasks(self):
        """Cancel all pending scheduled tasks. Call during shutdown.

        Persistence writes tracked in ``_persist_tasks`` are never cancelled
        here — they are drained (awaited to completion) by
        ``drain_persistence()`` instead, so a shutdown cannot truncate an
        in-flight session/inventory save.
        """
        for task in self._pending_tasks:
            if not task.done() and task not in self._persist_tasks:
                task.cancel()
        self._pending_tasks.clear()
        self._cancel_dispense_timeout()
        self._cancel_session_timeout()
        for pending in self._pending_refunds.values():
            if pending.deadline_task and not pending.deadline_task.done():
                pending.deadline_task.cancel()
        logger.debug("VMC: all pending tasks cancelled.")

    def set_mqtt_client(self, client):
        """Attach an MQTTClient instance for publishing status and receiving events."""
        self._mqtt_client = client
        # Register handlers for inbound ESP32 messages
        client.register("payment/credit", self._handle_mqtt_payment)
        client.register("hardware/buttons", self._handle_mqtt_button)
        client.register("hardware/dispenser", self._handle_mqtt_dispenser)
        client.register("sensors/temp/+", self._handle_mqtt_sensor)
        client.register("heartbeat/+", self._handle_mqtt_heartbeat)
        client.register("ice_maker/event", self._handle_mqtt_ice_maker_event)
        client.register("capabilities/+", self._handle_mqtt_capabilities)
        client.register("telemetry/ice_maker/+", self._handle_mqtt_telemetry)
        client.register("cmd/ice_maker/ack", self._handle_mqtt_command_ack)
        client.register("hardware/io/+", self._handle_mqtt_hardware_io)
        client.register("cmd/payment/refund/ack", self._handle_mqtt_refund_ack)
        client.register("payment/status", self._handle_mqtt_payment_status)
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
                self._raise_fault(code, outcome="heartbeat_lost")
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
            self._raise_fault(FaultCode.COM_103, outcome="disconnected")

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
        snap = store.load()
        if snap is not None and snap.is_open():
            self._flag_uncertain_session(snap)
        elif snap is not None:
            store.clear()
        logger.debug("VMC attached session store.")

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
        self._raise_fault(FaultCode.PAY_104, outcome=detail)

    def reconcile_session(self) -> None:
        """Future hook: query the payment gateway for held credit and clear
        PAY-104 automatically. The contract has no credit query yet, so the
        operator clears the fault from the dashboard after checking the machine.
        """
        return None

    def _snapshot(self, state: str | None = None) -> SessionSnapshot:
        pending = next(iter(self._pending_refunds), None)
        product = self.selected_product
        return SessionSnapshot(
            state=state or self.state,
            credit_escrow=round(self.credit_escrow, 2),
            selected_sku=product.sku if product else None,
            dispense_slot=product.slot
            if product and (state or self.state) == "dispensing"
            else None,
            dispense_started_at=time.time()
            if (state or self.state) == "dispensing"
            else None,
            pending_refund_request_id=pending,
        )

    def _persist_session(self, state: str | None = None) -> None:
        """Save the live session, or remove the file once nothing is in flight."""
        if self._session_store is None:
            return
        if FaultCode.PAY_104 in self._machine_faults:
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
        if self._mqtt_client is None or self._loop is None:
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

    def _product_name(self, sku: str | None) -> str | None:
        if sku is None:
            return None
        return next((p.name for p in self.products if p.sku == sku), sku)

    def _sellable_products(self) -> list:
        return [p for p in self.products if p.sku not in self._lockouts]

    def active_faults(self) -> list[dict]:
        """Snapshot for the dashboard/health monitor. Product faults first."""
        out = []
        for sku, code in self._lockouts.items():
            spec = FAULT_TABLE[code]
            out.append(
                {
                    "key": sku,
                    "sku": sku,
                    "product": self._product_name(sku),
                    "code": code.value,
                    "severity": spec.severity.value,
                    "scope": spec.scope.value,
                    "description": spec.description,
                }
            )
        for code in self._machine_faults:
            spec = FAULT_TABLE[code]
            out.append(
                {
                    "key": code.value,
                    "sku": None,
                    "product": None,
                    "code": code.value,
                    "severity": spec.severity.value,
                    "scope": spec.scope.value,
                    "description": spec.description,
                }
            )
        return out

    def _push_active_faults(self) -> None:
        faults = self.active_faults()
        if self._health_monitor:
            self._health_monitor.set_active_faults(faults)
        if self._availability:
            self._availability.set_active_faults(faults)

    def _raise_fault(
        self,
        code: FaultCode,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record a fault: lock the product if its severity says so, alert the owner."""
        spec = FAULT_TABLE[code]
        locks = spec.severity in (Severity.lockout, Severity.product_unavailable)
        if spec.scope is Scope.product and sku is not None and locks:
            if self._lockouts.get(sku) != code:
                self._lockouts[sku] = code
                if self._event_recorder:
                    self._event_recorder.record(
                        "lockout_set", metadata={"code": code.value, "sku": sku}
                    )
        elif spec.scope is Scope.machine:
            self._machine_faults.setdefault(code, time.monotonic())

        name = self._product_name(sku)
        message = f"{code.value} {spec.description}"
        if name:
            message += f" — product '{name}'"
        if outcome:
            message += f" (reported: {outcome})"
        logger.error(f"FAULT {message}")

        key = f"{code.value}:{sku or 'machine'}"
        level = _SEVERITY_LEVEL[spec.severity]
        if self._health_monitor:
            self._fire_and_forget(
                self._health_monitor.raise_alert(
                    key, level, "vmc", message, code=code.value, product_sku=sku
                )
            )
        if self._mqtt_client:
            self._fire_and_forget(
                self._mqtt_client.publish(
                    "alerts",
                    VMCAlert(level=level, message=message, code=code, product_sku=sku),
                )
            )
        self._push_active_faults()

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key (SKU for product faults, code string for machine faults)."""
        code = self._lockouts.pop(key, None)
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
        else:
            try:
                code = FaultCode(key)
            except ValueError:
                return False
            if code not in self._machine_faults:
                return False
            if code is FaultCode.PAY_104 and self._session_store:
                if not self._session_store.clear():
                    logger.error(
                        f"Fault {code.value}: could not remove session evidence file; "
                        "leaving fault in place."
                    )
                    return False
            del self._machine_faults[code]
            if code is FaultCode.PAY_104:
                if self._availability:
                    self._availability.set_transaction_certain(True)
            if self._health_monitor:
                self._health_monitor.clear_alert(f"{code.value}:machine")
            logger.info(f"Machine fault {code.value} cleared ({by})")
        self._push_active_faults()
        self._publish_status()
        return True

    async def _handle_mqtt_hardware_io(self, topic: str, data: dict):
        """Binary hardware IO from the vending ESP32; ice returning clears ICE-101."""
        hw = HardwareIO.model_validate(data)
        if self._availability:
            self._availability.set_hardware_io(hw.device, hw.state)
        if hw.device == "bin_half_full" and hw.state:
            for sku, code in list(self._lockouts.items()):
                if code is FaultCode.ICE_101:
                    self.clear_fault(sku, by="auto")
        else:
            logger.debug(f"MQTT hardware IO: {hw.device}={hw.state}")

    # --- MQTT inbound handlers ---

    async def _handle_mqtt_payment(self, topic: str, data: dict):
        """Handle payment credit from MDB ESP32."""
        event = PaymentEvent.model_validate(data)
        logger.info(f"MQTT payment received: ${event.amount:.2f} via {event.method}")
        txn_log.info(f"PAYMENT RECEIVED: ${event.amount:.2f} via {event.method}")
        self.deposit_funds(event.amount, payment_method=event.method)

    async def _handle_mqtt_payment_status(self, topic: str, data: dict):
        """MDB device readiness; any device in error/offline blocks payment."""
        status = PaymentStatus.model_validate(data)
        logger.debug(f"MQTT payment status: {status.device}={status.state}")
        if self._availability:
            self._availability.set_payment_device(status.device, status.state)

    async def _handle_mqtt_button(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        logger.info(f"MQTT button press: button {press.button}")
        txn_log.info(f"BUTTON PRESS: button {press.button}")
        vend_log.info(f"BUTTON PRESS: button {press.button}")
        self.select_product(press.button)

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

    async def _handle_mqtt_dispenser(self, topic: str, data: dict):
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

        product_name = (
            self.selected_product.name if self.selected_product else "Unknown"
        )
        if outcome is DispenserOutcome.complete:
            txn_log.info(f"DISPENSE SUCCESS: slot {slot}, product '{product_name}'")
            vend_log.info(f"DISPENSE COMPLETE: slot {slot}, product '{product_name}'")
            if self._event_recorder and self.selected_product:
                self._event_recorder.record(
                    "dispense", value=float(self.selected_product.slot)
                )
            self._finish_dispensing()
            return

        self._cancel_dispense_timeout()
        code = OUTCOME_FAULTS[outcome]
        sku = self.selected_product.sku if self.selected_product else None
        txn_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        vend_log.error(
            f"DISPENSE FAILED: slot {slot}, product '{product_name}', "
            f"outcome: {outcome.value}, fault: {code.value}"
        )
        self._raise_fault(code, sku=sku, outcome=outcome.value)
        self._fail_vend(code, outcome=outcome.value)

    async def _handle_mqtt_sensor(self, topic: str, data: dict):
        """Handle temperature/sensor reading from ESP32."""
        logger.debug(f"MQTT sensor [{topic}]: {data}")
        if self._health_monitor:
            location = data.get(
                "location", topic.split("/")[-1] if "/" in topic else topic
            )
            value = data.get("value")
            if value is not None:
                self._health_monitor.record_temperature(location, float(value))

    async def _handle_mqtt_heartbeat(self, topic: str, data: dict):
        """Handle heartbeat from ESP32 subsystem."""
        logger.debug(f"MQTT heartbeat [{topic}]: {data}")
        if self._health_monitor:
            subsystem = data.get(
                "subsystem", topic.split("/")[-1] if "/" in topic else topic
            )
            if data.get("uptime_seconds") == -1:
                logger.warning(
                    f"Subsystem '{subsystem}' reported OFFLINE (MQTT last will)"
                )
                self._health_monitor.mark_offline(subsystem)
                return
            self._health_monitor.record_heartbeat(subsystem, data)

    # Events logged to the ice maker log: power cycles, ice drops, out-of-spec
    _ICE_LOG_EVENTS = {
        "power_on",
        "power_off",
        "ice_dropped",
        "needs_cleaning",
        "failed_cycle",
        "temp_out_of_bounds",
    }

    async def _handle_mqtt_ice_maker_event(self, topic: str, data: dict):
        """Handle operational events from the ice maker ESP32."""
        event = IceMakerEvent.model_validate(data)
        logger.info(f"MQTT ice maker event: {event.event} — {event.detail or ''}")
        if event.event in self._ICE_LOG_EVENTS:
            detail = f" ({event.detail})" if event.detail else ""
            ice_log.info(f"{event.event.upper()}{detail}")

    async def _handle_mqtt_capabilities(self, topic: str, data: dict):
        """Store a subsystem's retained self-description and hand it to health."""
        subsystem = data.get("subsystem") or topic.split("/")[-1]
        try:
            caps = SubsystemCapabilities.model_validate(data)
            logger.info(
                f"Capabilities registered for '{subsystem}' "
                f"(firmware {caps.firmware}, contract {caps.contract_version}, "
                f"{len(caps.channels)} channels)"
            )
        except ValidationError:
            logger.warning(
                f"Capabilities for '{subsystem}' don't match the known schema; "
                "storing raw payload"
            )
        self.subsystem_capabilities[subsystem] = data
        if self._health_monitor:
            self._health_monitor.record_capabilities(subsystem, data)

    async def _handle_mqtt_telemetry(self, topic: str, data: dict):
        """Route a generic telemetry channel reading into health tracking."""
        reading = ChannelReading.model_validate(data)
        if self._health_monitor:
            self._health_monitor.record_channel(reading.channel_id, reading.value)

    async def _handle_mqtt_command_ack(self, topic: str, data: dict):
        """Log command acknowledgements from the monitor."""
        ack = CommandAck.model_validate(data)
        detail = f" — {ack.detail}" if ack.detail else ""
        logger.info(
            f"Monitor ack: {ack.command} -> {ack.status}{detail} ({ack.request_id})"
        )

    def _fire_and_forget(self, coro, *, persistent: bool = False) -> None:
        """Run a coroutine on the attached loop without awaiting it.

        The task is kept in _pending_tasks (so it is not garbage-collected and
        is cancelled on shutdown) and any exception it raises is logged rather
        than silently dropped — these carry alerts and refund commands.

        Pass persistent=True for session/inventory writes that must not be
        cancelled by a graceful shutdown; such tasks are additionally tracked
        in _persist_tasks so drain_persistence() can await them.
        """
        if self._loop is None or self._loop.is_closed():
            coro.close()
            return
        task = self._loop.create_task(coro)
        task.add_done_callback(self._log_task_failure)
        self._pending_tasks.append(task)
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]
        if persistent:
            self._persist_tasks.append(task)
            self._persist_tasks = [t for t in self._persist_tasks if not t.done()]

    async def drain_persistence(self, timeout: float = 3.0) -> None:
        """Await in-flight session/inventory writes so shutdown never cancels them."""
        pending = [t for t in self._persist_tasks if not t.done()]
        if not pending:
            return
        done, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.warning(
                f"Shutdown: {len(still)} persistence task(s) still running after {timeout}s"
            )

    @staticmethod
    def _log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Background task {task.get_name()} failed: {exc!r}")

    def _schedule(self, delay_seconds, callback) -> asyncio.Task | None:
        """Schedule a synchronous callback to run after delay_seconds on the event loop."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("No event loop attached; cannot schedule callback.")
            return None

        async def _delayed():
            await asyncio.sleep(delay_seconds)
            callback()

        task = self._loop.create_task(_delayed())
        self._pending_tasks.append(task)
        # Clean up finished tasks
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]
        return task

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
        return self.credit_escrow > 0

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
        self._cancel_session_timeout()
        self._update_display("dispensing")
        self._refresh_ui()
        self.send_customer_message(
            "Processing your payment and dispensing your product..."
        )
        # Tell the vending ESP32 which slot to dispense. Use the product's own
        # stable `slot` field, NOT its position in self.products — deleting an
        # earlier product from the catalog shifts list indices but must not
        # change which physical motor/slot a remaining product dispenses from.
        if self._mqtt_client and self._loop and self.selected_product:
            slot = self.selected_product.slot
            vend_log.info(
                f"DISPENSE CMD: slot {slot}, product '{self.selected_product.name}'"
            )
            snap = self._snapshot("dispensing") if self._session_store else None
            self._fire_and_forget(
                self._persist_then_dispense(snap, DispenseCommand(slot=slot)),
                persistent=True,
            )

    async def _persist_then_dispense(self, snap, cmd: DispenseCommand) -> None:
        """Write the dispensing snapshot to disk before the ESP32 is told to move.

        A crash between the two leaves an open session on disk, so boot raises
        PAY-104 instead of forgetting that credit was taken and a vend was
        in flight.
        """
        if snap is not None and self._session_store is not None:
            if FaultCode.PAY_104 not in self._machine_faults:
                await self._session_store.save_async(snap)
        await self._mqtt_client.publish("cmd/dispense", cmd)

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
        # _handle_mqtt_dispenser is the primary fix; clearing here removes the stale
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

        Restores the price to escrow (it was deducted in _process_payment),
        records the failure, and clears the selection. Whether the customer
        stays to choose again or is paid out is decided in _fail_vend.
        """
        product = self.selected_product
        price = product.price if product else 0.0
        name = product.name if product else "Unknown"
        sku = product.sku if product else None
        self._cancel_dispense_timeout()
        self.credit_escrow += price
        logger.error(
            f"{STATE_CHANGE_PREFIX} Vend failed for '{name}' ({code.value}, {outcome}); "
            f"${price:.2f} returned to escrow"
        )
        txn_log.error(
            f"VEND FAILED: '{name}' {code.value} ({outcome}); ${price:.2f} returned to escrow"
        )
        if self._event_recorder:
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
        self.vend_failed(code=code, outcome=outcome)
        if not self._sellable_products():
            txn_log.info("No sellable products remain; refunding and returning to idle")
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
            f"Dispense timed out after {self._dispense_timeout_seconds:.0f}s with no "
            f"terminal report (slot {self.selected_product.slot if self.selected_product else '?'})"
        )
        self._raise_fault(FaultCode.PAY_102, sku=sku, outcome="no_report")
        self._fail_vend(FaultCode.PAY_102, outcome="no_report")

    @logger.catch()
    def on_error(self):
        logger.error(
            f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state."
        )
        if self._event_recorder:
            self._event_recorder.record("error", value=1.0)
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
        if self._availability and not self._availability.payment_enabled:
            logger.warning(
                f"Credit ${amount:.2f} arrived while payment is disabled "
                f"({', '.join(self._availability.payment_blocking_reasons())}); "
                "escrowed"
            )
        self.credit_escrow += amount
        self.last_payment_method = payment_method
        logger.info(
            f"Deposited ${amount:.2f} via {payment_method}. New escrow: ${self.credit_escrow:.2f}"
        )
        if self.state == "interacting_with_user":
            self._reset_session_timeout()
        self._publish_status()
        self._refresh_ui()
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
        amount = round(self.credit_escrow, 2)
        self.credit_escrow = 0.0
        pending = PendingRefund(request_id=uuid4().hex, amount=amount, reason=reason)
        self._pending_refunds[pending.request_id] = pending
        self._send_refund_command(pending)
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

    def _send_refund_command(self, pending: PendingRefund) -> None:
        cmd = PaymentRefundCommand(
            request_id=pending.request_id, amount=pending.amount, reason=pending.reason
        )
        if self._mqtt_client is not None:
            self._fire_and_forget(self._mqtt_client.publish("cmd/payment/refund", cmd))
        else:
            logger.warning("No MQTT client; refund command not sent")
        pending.deadline_task = self._schedule(
            self.REFUND_ACK_TIMEOUT, lambda: self._refund_deadline(pending.request_id)
        )

    async def _handle_mqtt_refund_ack(self, topic: str, data: dict):
        """Payment gateway acknowledged (or refused) a refund command."""
        result = PaymentRefundResult.model_validate(data)
        pending = self._pending_refunds.get(result.request_id)
        if pending is None:
            logger.warning(f"Refund ack for unknown request_id {result.request_id}")
            return
        if result.status is RefundStatus.ok:
            self._refund_confirmed(pending, result.amount_returned)
        else:
            self._refund_attempt_failed(
                pending, detail=result.detail or result.status.value
            )

    def _cancel_refund_deadline(self, pending: PendingRefund) -> None:
        if pending.deadline_task and not pending.deadline_task.done():
            pending.deadline_task.cancel()
        pending.deadline_task = None

    def _refund_confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._cancel_refund_deadline(pending)
        self._pending_refunds.pop(pending.request_id, None)
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

    def _refund_deadline(self, request_id: str) -> None:
        pending = self._pending_refunds.get(request_id)
        if pending is None:
            return
        pending.deadline_task = None
        self._refund_attempt_failed(pending, detail="ack_timeout")

    def _refund_attempt_failed(self, pending: PendingRefund, detail: str) -> None:
        self._cancel_refund_deadline(pending)
        if pending.attempts < self.REFUND_MAX_ATTEMPTS:
            pending.attempts += 1
            logger.warning(
                f"Refund {pending.request_id} not confirmed ({detail}); "
                f"retry {pending.attempts}/{self.REFUND_MAX_ATTEMPTS}"
            )
            self._send_refund_command(pending)
            return
        self._pending_refunds.pop(pending.request_id, None)
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
        self._raise_fault(FaultCode.PAY_103, outcome=detail)

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
        locked_code = self._lockouts.get(candidate.sku)
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
            self._schedule(1.0, self._process_payment)
        elif self.state == "interacting_with_user":
            self.initiate_virtual_payment(self.selected_product.price)
            self._schedule(1.0, self._process_payment)
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
    def _process_payment(self):
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
            self.credit_escrow -= price
            logger.debug(
                f"Deducted price from escrow. New escrow: {self.credit_escrow:.2f}"
            )
            self.dispense_product()
            self._persist_session("dispensing")
            self._refresh_ui()
            # Dispenser hardware reports a terminal DispenserOutcome via MQTT; no
            # report within the timeout is a failed vend (PAY-102).
            self._dispense_timeout_task = self._schedule(
                self._dispense_timeout_seconds, self._dispense_timed_out
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
            self._schedule(5.0, self._process_payment)

    def _reset_session_timeout(self):
        """Reset (or start) the customer session inactivity timer."""
        if self._session_timeout_task and not self._session_timeout_task.done():
            self._session_timeout_task.cancel()
        self._session_timeout_task = self._schedule(
            self._session_timeout_seconds, self._expire_session
        )

    def _cancel_session_timeout(self):
        """Cancel the session timeout (e.g., when dispensing starts)."""
        if self._session_timeout_task and not self._session_timeout_task.done():
            self._session_timeout_task.cancel()
        self._session_timeout_task = None

    @logger.catch()
    def _expire_session(self):
        """Called when the customer session times out due to inactivity."""
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
