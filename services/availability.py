"""Availability permissives: decides when the machine may accept money.

ROADMAP.md §3. Inputs are pushed in by the VMC and the health monitor; this
module computes per-kind sale availability and publishes cmd/payment/enable
whenever the overall answer changes. Inputs the hardware cannot report yet are
present as "not instrumented" rows that always pass, so flipping one to
fail-closed later is a one-line change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from loguru import logger

from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS


class PermissiveState(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Applies(str, Enum):
    ice = "ice"
    water = "water"
    both = "both"


class Gate(str, Enum):
    """How far a failing permissive reaches.

    safety      — a physical hazard. Blocks payment and every sale.
    fulfillment — the machine cannot complete this sale right now. Blocks the
                  sale; payment stays enabled so a transient heartbeat gap or
                  broker reconnect never costs a night of revenue.
    alert       — the operator should know, but nothing is blocked.
    """

    safety = "safety"
    fulfillment = "fulfillment"
    alert = "alert"


@dataclass
class Permissive:
    name: str
    applies_to: Applies
    instrumented: bool
    state: PermissiveState
    detail: str = ""
    gate: Gate = Gate.fulfillment

    def as_row(self) -> dict:
        return {
            "name": self.name,
            "applies_to": self.applies_to.value,
            "instrumented": self.instrumented,
            "state": self.state.value,
            "detail": self.detail,
            "gate": self.gate.value,
        }


# Heartbeat subsystem name -> permissive it drives.
LIVENESS_INPUTS = {
    "vending": "vending_alive",
    "mdb": "payment_alive",
    "ice_maker": "ice_maker_alive",
}

# active_faults() reports codes as strings; compare against the contract set.
_PAYMENT_BLOCKING_CODES = {code.value for code in PAYMENT_BLOCKING_FAULTS}
_BAD_DEVICE_STATES = {"error", "offline"}


def _inst(
    name: str,
    applies: Applies,
    state: PermissiveState = PermissiveState.UNKNOWN,
    detail: str = "",
    gate: Gate = Gate.fulfillment,
) -> Permissive:
    return Permissive(name, applies, True, state, detail, gate)


def _stub(name: str, applies: Applies, gate: Gate = Gate.fulfillment) -> Permissive:
    return Permissive(
        name, applies, False, PermissiveState.PASS, "not instrumented", gate
    )


class Availability:
    """Truth table of permissives plus the payment/enable publisher.

    Usage:
        avail = Availability()
        avail.set_publisher(vmc.publish_payment_enable)   # sync callable(bool)
        avail.set_subsystem_alive("vending", True)         # ... from health monitor
        ok, failing = avail.product_sellable(product)
    """

    def __init__(self):
        self._lockouts: dict[str, str] = {}
        self._publish: Optional[Callable[[bool], None]] = None
        self._recorder = None
        self._payment_devices: dict[str, str] = {}
        self._bin_half_full: Optional[bool] = None
        self._ice_101_active = False
        self._last_published: Optional[bool] = None
        rows = [
            _inst("mqtt_connected", Applies.both),
            _inst("vending_alive", Applies.both),
            _inst("payment_alive", Applies.both),
            _inst("payment_devices_ready", Applies.both),
            _inst("ice_maker_alive", Applies.ice),
            _inst("ice_available", Applies.ice, detail="no bin report yet"),
            _inst("fsm_ok", Applies.both),
            _inst(
                "no_critical_fault",
                Applies.both,
                PermissiveState.PASS,
                gate=Gate.safety,
            ),
            _inst(
                "service_door_closed",
                Applies.both,
                PermissiveState.PASS,
                "assumed closed; no report yet",
                gate=Gate.safety,
            ),
            _inst(
                "transaction_certain",
                Applies.both,
                PermissiveState.PASS,
                gate=Gate.alert,
            ),
            _stub("bag_present", Applies.ice),
            _stub("trap_door_closed", Applies.ice, gate=Gate.safety),
            _stub("control_power_ok", Applies.both, gate=Gate.safety),
            _stub("water_pressure_ok", Applies.water),
            _stub("water_treatment_ok", Applies.water),
            _stub("no_leak", Applies.water, gate=Gate.safety),
            _stub("water_valve_closed", Applies.water, gate=Gate.safety),
        ]
        self._rows: dict[str, Permissive] = {r.name: r for r in rows}

    # --- wiring ---

    def set_publisher(self, publish: Callable[[bool], None]) -> None:
        self._publish = publish
        self._recompute()

    def set_event_recorder(self, recorder) -> None:
        self._recorder = recorder

    # --- inputs ---

    def _set(self, name: str, state: PermissiveState, detail: str = "") -> None:
        row = self._rows[name]
        row.state = state
        row.detail = detail
        self._recompute()

    def _set_bool(self, name: str, ok: bool, fail_detail: str = "") -> None:
        self._set(
            name,
            PermissiveState.PASS if ok else PermissiveState.FAIL,
            "" if ok else fail_detail,
        )

    def set_mqtt_connected(self, connected: bool) -> None:
        self._set_bool("mqtt_connected", connected, "broker disconnected")

    def set_subsystem_alive(self, subsystem: str, alive: bool) -> None:
        name = LIVENESS_INPUTS.get(subsystem)
        if name is None:
            return
        self._set_bool(name, alive, f"{subsystem} heartbeat lost")

    def set_payment_device(self, device: str, state: str) -> None:
        self._payment_devices[device] = state
        bad = sorted(
            d for d, s in self._payment_devices.items() if s in _BAD_DEVICE_STATES
        )
        self._set_bool("payment_devices_ready", not bad, ", ".join(bad))

    def set_fsm_state(self, state: str) -> None:
        self._set_bool("fsm_ok", state != "error", "VMC in error state")

    def set_active_faults(self, faults: list[dict]) -> None:
        """Consume VMC.active_faults(): lockouts, machine faults, ICE-101."""
        self._lockouts = {
            f["sku"]: f["code"]
            for f in faults
            if f.get("scope") == "product" and f.get("sku")
        }
        self._ice_101_active = any(f.get("code") == "ICE-101" for f in faults)
        blocking = sorted(
            f["code"]
            for f in faults
            if f.get("scope") == "machine" and f.get("code") in _PAYMENT_BLOCKING_CODES
        )
        self._rows["no_critical_fault"].state = (
            PermissiveState.FAIL if blocking else PermissiveState.PASS
        )
        self._rows["no_critical_fault"].detail = ", ".join(blocking)
        self._refresh_ice_available()
        self._recompute()

    def set_hardware_io(self, device: str, state: bool) -> None:
        if device == "bin_half_full":
            self._bin_half_full = state
            self._refresh_ice_available()
            self._recompute()
        elif device == "service_door":
            self._set_bool("service_door_closed", not state, "service door open")

    def set_transaction_certain(self, certain: bool) -> None:
        self._set_bool("transaction_certain", certain, "PAY-104 active")

    def _refresh_ice_available(self) -> None:
        row = self._rows["ice_available"]
        if self._ice_101_active:
            row.state, row.detail = PermissiveState.FAIL, "ICE-101 active"
        elif self._bin_half_full is None:
            row.state, row.detail = PermissiveState.UNKNOWN, "no bin report yet"
        elif self._bin_half_full:
            row.state, row.detail = PermissiveState.PASS, ""
        else:
            row.state, row.detail = PermissiveState.FAIL, "bin empty"

    # --- outputs ---

    def _rows_for(self, kind: str) -> list[Permissive]:
        """Rows that can block a sale of *kind*. Alert rows never block."""
        rows = [r for r in self._rows.values() if r.gate is not Gate.alert]
        if kind in ("ice", "water"):
            return [r for r in rows if r.applies_to in (Applies.both, Applies(kind))]
        return rows

    def sale_available(self, kind: str) -> tuple[bool, list[str]]:
        failing = [
            r.name for r in self._rows_for(kind) if r.state is not PermissiveState.PASS
        ]
        return (not failing, failing)

    def product_sellable(self, product) -> tuple[bool, list[str]]:
        ok, failing = self.sale_available(getattr(product, "kind", "other"))
        code = self._lockouts.get(product.sku)
        if code:
            failing = failing + [f"lockout:{code}"]
            ok = False
        return ok, failing

    def payment_blocking_reasons(self) -> list[str]:
        """Failing safety rows — the only reasons payment may be inhibited.

        Safety is machine-wide: a leak or a bad 24 V supply stops the whole
        machine, regardless of which product kind the row nominally applies to.
        """
        return sorted(
            r.name
            for r in self._rows.values()
            if r.gate is Gate.safety and r.state is not PermissiveState.PASS
        )

    @property
    def payment_enabled(self) -> bool:
        return not self.payment_blocking_reasons()

    def table(self) -> list[dict]:
        rows = sorted(self._rows.values(), key=lambda r: (not r.instrumented, r.name))
        return [r.as_row() for r in rows]

    def republish(self) -> None:
        """Send the current value even if unchanged (MQTT reconnect, gateway back)."""
        if self._publish is None:
            return
        enabled = self.payment_enabled
        self._last_published = enabled
        self._publish(enabled)

    def _recompute(self) -> None:
        enabled = self.payment_enabled
        if enabled == self._last_published:
            return
        self._last_published = enabled
        reasons = self.payment_blocking_reasons()
        logger.info(
            f"Availability: payment {'ENABLED' if enabled else 'DISABLED'}"
            + (f" ({', '.join(reasons)})" if reasons else "")
        )
        if self._recorder:
            self._recorder.record(
                "availability_changed",
                value=1.0 if enabled else 0.0,
                metadata={"enabled": enabled, "failing": reasons},
            )
        if self._publish is not None:
            self._publish(enabled)
