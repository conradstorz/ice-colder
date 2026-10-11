"""Fault side effects: alerts, event-recorder rows, health/availability
pushes, and the liveness/MQTT-connection fault mappings -- the third piece
carved off the VMC god object, following the same pattern as
``controller/fault_registry.py``'s ``FaultRegistry`` (the pure bookkeeping
this class wraps), ``controller/outputs.py``'s ``StatusOutputs`` (the
FSM's outbound channel, Task 1), and ``controller/task_runner.py``'s
``TaskRunner`` (see ``CLAUDE.md``'s "FSM Core" section).

``FaultService`` owns ``raise_fault``/``clear_fault`` (today's
``VMC.raise_fault``/``VMC.clear_fault`` bodies verbatim, including the
SVC-102 lease guard, the PAY-104 session-evidence guard, and the CFG-101
re-lock check), the two inbound event handlers that drive fault state from
liveness/connection changes (``on_subsystem_liveness``, keyed by
``_LIVENESS_FAULTS``, and ``on_mqtt_connection``), ``clear_ice101_lockouts``,
and read-only pass-throughs onto the registry
(``is_locked``/``has``/``lockouts``/``machine_faults``/``active_faults``/
``parse_key``) plus the health/availability push (``push_active_faults``,
public here -- both ``raise_fault``/``clear_fault`` above and
``Machine.set_availability`` call it).

Five collaborators are injected as callables rather than objects, because
the VMC attaches (or replaces) what they read after this class is
constructed: ``recorder`` returns the live event recorder (or ``None``),
``lease_holder`` returns the maintenance lease's current holder user id
(or ``None`` -- the lease's own ``holder_user_id`` is never itself
``None`` once a hold exists, so this callable's ``None``/not-``None``
split is exactly the original's ``self._lease.hold is not None`` guard),
``lacks_valid_profile`` answers the CFG-101 re-check, ``set_transaction_certain``
forwards to availability, and ``fsm_state`` returns the live FSM state
string. Health, MQTT, and availability are *not* injected this way --
they are read from ``outputs.health``/``outputs.mqtt``/
``outputs.availability`` at call time, exactly as ``StatusOutputs`` itself
reads its own sinks.

``set_transaction_certain`` must be a closure that reads availability at
call time (e.g. ``lambda certain: vmc._availability and
vmc._availability.set_transaction_certain(certain)``), not a bound method
like ``self._availability.set_transaction_certain`` captured once --
availability is attached to the VMC *after* construction (via
``set_availability``), and the original call site was itself guarded with
``if self._availability:``; this class calls the callable unconditionally
on a successful PAY-104 clear, so the guard belongs inside the closure the
caller supplies.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from contracts.vending_machine import FaultCode
from controller.fault_registry import FaultRegistry
from controller.outputs import StatusOutputs
from controller.task_runner import TaskRunner
from services.mqtt_messages import VMCAlert

# Heartbeat loss per subsystem -> registry fault (ROADMAP §5, §8). Moved
# here from controller/vmc.py verbatim.
_LIVENESS_FAULTS = {
    "vending": FaultCode.COM_101,
    "ice_maker": FaultCode.COM_102,
    "mdb": FaultCode.PAY_101,
}


class FaultService:
    """Side effects layered on top of a ``FaultRegistry``: event-recorder
    rows, health alerts, MQTT publishes, and the availability/health
    active-fault push -- plus the inbound liveness/connection handlers
    that raise and clear faults from elsewhere in the system."""

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        outputs: StatusOutputs,
        tasks: TaskRunner,
        recorder: Callable[[], object | None],
        lease_holder: Callable[[], str | None],
        lacks_valid_profile: Callable[[str], bool],
        set_transaction_certain: Callable[[bool], None],
        fsm_state: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._outputs = outputs
        self._tasks = tasks
        self._recorder = recorder
        self._lease_holder = lease_holder
        self._lacks_valid_profile = lacks_valid_profile
        self._set_transaction_certain = set_transaction_certain
        self._fsm_state = fsm_state

    # --- mutators ---

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
        DATA-102).
        """
        raised = self._registry.raise_fault(code, sku=sku, outcome=outcome)
        recorder = self._recorder()
        if raised.newly_locked and recorder:
            recorder.record("lockout_set", metadata={"code": code.value, "sku": sku})
        logger.error(f"FAULT {raised.message}")

        health = self._outputs.health
        if health:
            self._tasks.fire_and_forget(
                health.raise_alert(
                    raised.alert_key,
                    raised.level,
                    "vmc",
                    raised.message,
                    code=code.value,
                    product_sku=sku,
                )
            )
        self._outputs.publish_alert(
            VMCAlert(
                level=raised.level,
                message=raised.message,
                code=code,
                product_sku=sku,
            )
        )
        self.push_active_faults()

    def clear_fault(self, key: str, by: str = "admin") -> bool:
        """Clear a fault by key (SKU for product faults, code string for machine faults)."""
        code = self._registry.pop_lockout(key)
        if code is not None:
            sku = key
            recorder = self._recorder()
            if recorder:
                recorder.record(
                    "lockout_cleared",
                    metadata={"code": code.value, "sku": sku, "by": by},
                )
            health = self._outputs.health
            if health:
                health.clear_alert(f"{code.value}:{sku}")
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
            if self._lacks_valid_profile(sku):
                logger.info(f"{sku} re-locked: no valid dispenser profile (CFG-101)")
                self.raise_fault(FaultCode.CFG_101, sku=sku)
        else:
            code = self._registry.parse_key(key)
            if code is None:
                return False
            if not self._registry.has(code):
                return False
            if code is FaultCode.SVC_102:
                holder = self._lease_holder()
                if holder is not None:
                    # Copilot review (PR 22): a generic clear must not
                    # bypass the maintenance lease invariant, the same
                    # class of bug fixed twice already for PAY-104 in part
                    # 3. Only lease release (end_maintenance / idle
                    # timeout / the last in-flight run settling) may clear
                    # SVC-102; by the time that path calls clear_fault it
                    # has already released the lease, so this check
                    # cannot block the real release.
                    logger.warning(
                        "Refused generic clear of SVC-102: maintenance lease "
                        f"still held by {holder}"
                    )
                    return False
            if code is FaultCode.PAY_104:
                if not self._outputs.clear_session_evidence():
                    logger.error(
                        f"Fault {code.value}: could not remove session evidence file; "
                        "leaving fault in place."
                    )
                    return False
            self._registry.clear_machine(code)
            if code is FaultCode.PAY_104:
                self._set_transaction_certain(True)
            health = self._outputs.health
            if health:
                health.clear_alert(f"{code.value}:machine")
            logger.info(f"Machine fault {code.value} cleared ({by})")
        self.push_active_faults()
        self._outputs.state_changed(self._fsm_state())
        return True

    # --- inbound event handlers ---

    def on_subsystem_liveness(self, subsystem: str, alive: bool) -> None:
        """Heartbeat-loss callback from the health monitor (ROADMAP §5, §8)."""
        code = _LIVENESS_FAULTS.get(subsystem)
        if code is not None:
            if alive:
                self.clear_fault(code.value, by="auto")
            else:
                self.raise_fault(code, outcome="heartbeat_lost")
        availability = self._outputs.availability
        if availability:
            availability.set_subsystem_alive(subsystem, alive)
            if subsystem == "mdb" and alive:
                availability.republish()

    def on_mqtt_connection(self, connected: bool) -> None:
        """Connection-state callback from MQTTClient (chained after the health monitor)."""
        availability = self._outputs.availability
        if availability:
            availability.set_mqtt_connected(connected)
        if connected:
            self.clear_fault(FaultCode.COM_103.value, by="auto")
            if availability:
                availability.republish()
            self._outputs.state_changed(self._fsm_state())
        else:
            self.raise_fault(FaultCode.COM_103, outcome="disconnected")

    def clear_ice101_lockouts(self) -> None:
        """Clear every ICE-101 lockout -- invoked by the telemetry router
        (controller/mqtt_inbound.py's `TelemetryRouter.handle_hardware_io`)
        when the vending ESP32 reports `bin_half_full` going true."""
        for sku, code in list(self._registry.lockouts.items()):
            if code is FaultCode.ICE_101:
                self.clear_fault(sku, by="auto")

    # --- reads re-exposed from the registry ---

    def is_locked(self, sku: str) -> FaultCode | None:
        return self._registry.is_locked(sku)

    def has(self, code: FaultCode) -> bool:
        return self._registry.has(code)

    @property
    def lockouts(self) -> dict[str, FaultCode]:
        return self._registry.lockouts

    @property
    def machine_faults(self) -> dict[FaultCode, float]:
        return self._registry.machine_faults

    def active_faults(self) -> list[dict]:
        """Snapshot for the dashboard/health monitor. Product faults first."""
        return self._registry.snapshot()

    def parse_key(self, key: str) -> FaultCode | None:
        return self._registry.parse_key(key)

    def push_active_faults(self) -> None:
        faults = self.active_faults()
        health = self._outputs.health
        if health:
            health.set_active_faults(faults)
        availability = self._outputs.availability
        if availability:
            availability.set_active_faults(faults)
