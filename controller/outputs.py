"""Outbound side effects: MQTT publishes, session persistence, the
customer display, and the dashboard's UI-refresh/message/QR callbacks --
the FSM's only outbound channel.

Extracted from ``controller.vmc.VMC`` as the first piece carved off the
VMC god object (vmc-reduction plan, Task 1), following the same pattern as
``controller/fault_registry.py``'s ``FaultRegistry`` and the other
collaborators described in ``CLAUDE.md``'s "FSM Core" section.
``StatusOutputs`` knows nothing about the FSM, escrow, or sales -- every
sink (MQTT client, health monitor, availability, session store, display
controller) is attached later via its own ``attach_*`` method and defaults
to ``None``, so this class is fully usable -- every method either a safe
no-op or a logged warning -- with no sink attached at all, exactly as the
VMC is usable before ``main.py`` finishes wiring MQTT/health/availability/
session store/display onto it.

``snapshot``/``credit_escrow``/``selected_product``/``fsm_state``/
``pay104_active`` are callables read at call time, not snapshotted at
construction, mirroring the pattern already used for
``RefundProtocol.ack_timeout``/``max_attempts``: the live VMC's state
changes between construction and any given call, so this class must
always see the current value.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from config.config_model import Product
from contracts.vending_machine import PaymentRefundCommand
from controller.task_runner import TaskRunner
from services.availability import Availability
from services.display_controller import DisplayController
from services.health_monitor import HealthMonitor
from services.mqtt_messages import PaymentEnableCommand, VMCAlert, VMCStatus
from services.session_store import SessionSnapshot, SessionStore

if TYPE_CHECKING:
    from services.mqtt_client import MQTTClient


class StatusOutputs:
    """The FSM's only outbound channel.

    Constructed with five callables that always read live VMC state (never
    snapshotted) plus the shared ``TaskRunner`` used for every
    fire-and-forget publish/persist. Every sink is attached later via
    ``attach_mqtt``/``attach_health``/``attach_availability``/
    ``attach_session_store``/``attach_display`` and defaults to ``None``.
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[str | None], SessionSnapshot],
        credit_escrow: Callable[[], float],
        selected_product: Callable[[], Product | None],
        fsm_state: Callable[[], str],
        pay104_active: Callable[[], bool],
        tasks: TaskRunner,
    ) -> None:
        self._snapshot = snapshot
        self._credit_escrow = credit_escrow
        self._selected_product = selected_product
        self._fsm_state = fsm_state
        self._pay104_active = pay104_active
        self._tasks = tasks
        self._start_time = time.monotonic()

        self._mqtt = None
        self._health = None
        self._availability = None
        self._session_store = None
        self._display = None

        self.update_callback = None
        self.message_callback = None
        self.qrcode_callback = None

    # --- sinks ---

    def attach_mqtt(self, client: "MQTTClient") -> None:
        self._mqtt = client

    def attach_health(self, monitor: HealthMonitor) -> None:
        self._health = monitor

    def attach_availability(self, availability: Availability) -> None:
        self._availability = availability

    def attach_session_store(self, store: SessionStore) -> None:
        self._session_store = store

    def attach_display(self, controller: DisplayController) -> None:
        self._display = controller

    @property
    def mqtt(self):
        return self._mqtt

    @property
    def health(self):
        return self._health

    @property
    def availability(self):
        return self._availability

    @property
    def session_store(self):
        return self._session_store

    @property
    def display_controller(self):
        return self._display

    # --- callbacks ---

    def set_update_callback(self, callback) -> None:
        self.update_callback = callback

    def set_message_callback(self, callback) -> None:
        self.message_callback = callback

    def set_qrcode_callback(self, callback) -> None:
        self.qrcode_callback = callback

    # --- uptime ---

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._start_time)

    # --- state / persistence ---

    def state_changed(self, state: str) -> None:
        """Push a state change to health/availability, persist the
        session, and (only when both an MQTT client and a running loop are
        attached) publish a retained ``status`` message -- mirrors
        ``VMC._publish_status`` exactly, including the ordering: health
        and availability are pushed, and the session persisted,
        regardless of whether an MQTT client is attached."""
        if self._health:
            self._health.update_vmc_state(state)
        if self._availability:
            self._availability.set_fsm_state(state)
        self.persist(state)
        if self._mqtt is None or self._tasks.loop is None:
            return
        product = self._selected_product()
        status = VMCStatus(
            state=state,
            credit_escrow=self._credit_escrow(),
            selected_product=product.name if product else None,
            uptime_seconds=self.uptime_seconds,
        )
        self._tasks.fire_and_forget(self._mqtt.publish("status", status, retain=True))

    def persist(self, state: str | None = None) -> None:
        """Save the live session, or remove the file once nothing is in
        flight. A no-op without a session store; while PAY-104 is active
        the evidence file is left untouched for the operator."""
        if self._session_store is None:
            return
        if self._pay104_active():
            return
        snap = self._snapshot(state)
        if snap.is_open():
            self._tasks.fire_and_forget(
                self._session_store.save_async(snap), persistent=True
            )
        else:
            self._tasks.fire_and_forget(
                self._session_store.clear_async(), persistent=True
            )

    async def save_snapshot_async(self, snap: SessionSnapshot | None) -> None:
        """Await a snapshot save directly, for a caller that is already
        async and needs to await the write itself (the dispatcher-based
        dispense path) rather than fire-and-forget it. A no-op when
        ``snap`` is ``None`` -- mirroring the original call site's own
        guard (``snap is not None and self._session_store is not None``)
        -- plus the same two guards as ``persist``; any exception from the
        save propagates to the caller, which is responsible for its own
        failure handling."""
        if snap is None:
            return
        if self._session_store is None:
            return
        if self._pay104_active():
            return
        await self._session_store.save_async(snap)

    def clear_session_evidence(self) -> bool:
        """Remove the session evidence file. Returns ``True`` without a
        store (nothing to clear), else the store's own answer."""
        if self._session_store is None:
            return True
        return self._session_store.clear()

    # --- customer-facing display / UI ---

    def display(self, state: str) -> None:
        """Update the customer-facing display for the given FSM state."""
        if self._display:
            self._display.update_for_state(state)

    def message(self, text: str) -> None:
        """Send a message to the customer via the registered callback --
        mirrors ``VMC.send_customer_message`` exactly, including its log
        level and wording."""
        logger.debug(f"Sending customer message: '{text}'")
        if self.message_callback:
            self.message_callback(text)

    def refresh(self) -> None:
        """Tell the dashboard's registered callback to refresh, passing
        the live FSM state, selected product, and credit escrow --
        mirrors ``VMC._refresh_ui`` exactly, including its three-argument
        call."""
        if self.update_callback:
            self.update_callback(
                self._fsm_state(), self._selected_product(), self._credit_escrow()
            )

    def show_qr(self, image) -> None:
        """Hand a QR code image to the registered callback."""
        if self.qrcode_callback:
            self.qrcode_callback(image)

    # --- MQTT publishes ---

    def publish_payment_enable(self, accept: bool) -> None:
        """Sync publisher handed to Availability (fire-and-forget on the loop)."""
        if self._mqtt is None:
            logger.warning("No MQTT client; payment/enable not sent")
            return
        self._tasks.fire_and_forget(
            self._mqtt.publish(
                "cmd/payment/enable", PaymentEnableCommand(accept=accept)
            )
        )

    def publish_refund(self, cmd: PaymentRefundCommand) -> None:
        """Publish closure handed to RefundProtocol. Reads the attached
        client at call time, not construction time -- it is still absent
        when this class is built and only attached later."""
        if self._mqtt is None:
            logger.warning("No MQTT client; refund command not sent")
            return
        self._tasks.fire_and_forget(self._mqtt.publish("cmd/payment/refund", cmd))

    def publish_alert(self, alert: VMCAlert) -> None:
        """Publish an alert raised by the fault registry. Silently skipped
        without a client -- the alert is still raised to the health
        monitor and event recorder by the caller regardless."""
        if self._mqtt is None:
            return
        self._tasks.fire_and_forget(self._mqtt.publish("alerts", alert))
