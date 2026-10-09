"""Fault registry: product-scope lockouts and machine-scope fault bookkeeping.

Extracted from ``controller.vmc.VMC`` as the first step in breaking up the
VMC god object (~3100 lines). ``FaultRegistry`` owns the two dicts
(``lockouts`` by sku, ``machine_faults`` by code), the severity -> alert
level map, and the pure bookkeeping of raising/clearing/snapshotting a
fault against ``contracts.vending_machine.FAULT_TABLE``. It knows nothing
about MQTT, the health monitor, the event recorder, the session store, or
the maintenance lease -- those side effects and VMC-specific guards stay
on ``VMC`` itself. See ``CLAUDE.md``'s "FSM Core" section.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from contracts.vending_machine import FAULT_TABLE, FaultCode, FaultSpec, Scope, Severity

# Alert level sent to the owner for each fault severity.
_SEVERITY_LEVEL = {
    Severity.info: "info",
    Severity.warning: "warning",
    Severity.product_unavailable: "warning",
    Severity.vend_failed: "warning",
    Severity.lockout: "error",
    Severity.critical: "critical",
}


def severity_level(severity: Severity) -> str:
    """Public accessor for the severity -> alert-level map."""
    return _SEVERITY_LEVEL[severity]


@dataclass(frozen=True)
class RaisedFault:
    """Result of :meth:`FaultRegistry.raise_fault`, enough for the caller
    to perform its side effects in the same order the VMC always has:
    an event-recorder ``lockout_set`` row (only when ``newly_locked``),
    an error log line, a health-monitor alert, and an MQTT publish --
    all using ``message``/``level``/``alert_key`` computed here.
    """

    code: FaultCode
    sku: str | None
    spec: FaultSpec
    newly_locked: bool
    message: str
    level: str
    alert_key: str


class FaultRegistry:
    """Holds the fault state the VMC used to keep directly on itself."""

    def __init__(self, product_name: Callable[[str | None], str | None]):
        # Bound method (or any callable) consulted at call time -- never
        # snapshotted -- so a catalog edit between raises is reflected
        # immediately, matching VMC._product_name's own semantics.
        self._product_name = product_name
        self.lockouts: dict[str, FaultCode] = {}
        self.machine_faults: dict[FaultCode, float] = {}

    def raise_fault(
        self,
        code: FaultCode,
        sku: str | None = None,
        outcome: str | None = None,
    ) -> RaisedFault:
        """Record a fault: lock the product if its severity says so,
        record a machine fault if its scope says so, and build the
        message/alert-key the caller needs for its own side effects.

        Scope decides the branch, not severity: a product-unavailable
        *machine*-scope code (e.g. PAY-101) never locks a product --
        it is recorded in ``machine_faults`` like any other machine
        fault. A product-scope code whose severity doesn't lock (e.g.
        vend_failed) and isn't machine-scope is recorded nowhere, by
        design -- it only alerts.
        """
        spec = FAULT_TABLE[code]
        locks = spec.severity in (Severity.lockout, Severity.product_unavailable)
        newly_locked = False
        if spec.scope is Scope.product and sku is not None and locks:
            if self.lockouts.get(sku) != code:
                self.lockouts[sku] = code
                newly_locked = True
        elif spec.scope is Scope.machine:
            self.machine_faults.setdefault(code, time.monotonic())

        name = self._product_name(sku)
        message = f"{code.value} {spec.description}"
        if name:
            message += f" — product '{name}'"
        if outcome:
            message += f" (reported: {outcome})"

        alert_key = f"{code.value}:{sku or 'machine'}"
        level = _SEVERITY_LEVEL[spec.severity]
        return RaisedFault(
            code=code,
            sku=sku,
            spec=spec,
            newly_locked=newly_locked,
            message=message,
            level=level,
            alert_key=alert_key,
        )

    def pop_lockout(self, sku: str) -> FaultCode | None:
        """Remove and return the lockout code for *sku*, or None."""
        return self.lockouts.pop(sku, None)

    def clear_machine(self, code: FaultCode) -> bool:
        """Remove a machine fault. Returns False if it wasn't active."""
        if code not in self.machine_faults:
            return False
        del self.machine_faults[code]
        return True

    def parse_key(self, key: str) -> FaultCode | None:
        """Parse a clear_fault key as a FaultCode, or None if it isn't one."""
        try:
            return FaultCode(key)
        except ValueError:
            return None

    def has(self, code: FaultCode) -> bool:
        """Whether a machine fault is currently active."""
        return code in self.machine_faults

    def is_locked(self, sku: str) -> FaultCode | None:
        """The lockout code currently held against *sku*, or None."""
        return self.lockouts.get(sku)

    def snapshot(self) -> list[dict]:
        """Dashboard/health-monitor snapshot. Product faults first, then
        machine faults, each in insertion order -- same shape
        VMC.active_faults() has always returned.
        """
        out = []
        for sku, code in self.lockouts.items():
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
        for code in self.machine_faults:
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
