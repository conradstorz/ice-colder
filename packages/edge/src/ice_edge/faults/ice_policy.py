"""Ice-availability policy: when ICE-101 (no ice) clears.

Two clears live here. With a bin sensor, ``bin_half_full`` going true means
ice is back, so every ICE-101 lockout is cleared (``by="auto"``). Without a
bin sensor nothing can say ice is back, so each ICE-101 raise arms a
``ice_retry_cooldown`` timer that clears the lockouts and lets the next sale
try again (``by="cooldown"``). An operator clear that leaves no ICE-101
lockout cancels the timer.

If the bin-sensor declaration changes after an ICE-101 was raised (the
capabilities document arrives late, or is replaced), ``on_bin_instrumented``
re-evaluates: losing the sensor arms the cooldown for lockouts already held;
gaining one cancels a pending timer, since the sensor now owns the clear.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ice_contracts.board.vending_machine import FaultCode, VendingChannel
from ice_edge.faults.availability import declares_bin_sensor
from ice_edge.faults.fault_service import FaultChange, FaultService
from ice_edge.supervisor.task_runner import TaskRunner

COOLDOWN_LABEL = "ice_retry_cooldown"


class IcePolicy:
    def __init__(
        self,
        *,
        faults: FaultService,
        tasks: TaskRunner,
        bin_instrumented: Callable[[], bool],
        cooldown_seconds: Callable[[], float],
    ) -> None:
        self._faults = faults
        self._tasks = tasks
        self._bin_instrumented = bin_instrumented
        self._cooldown_seconds = cooldown_seconds
        self._timer: asyncio.Task | None = None

    @property
    def pending(self) -> bool:
        return self._timer is not None

    def on_hardware_io(self, device: str, state: bool) -> None:
        if device == VendingChannel.bin_half_full and state:
            self._faults.clear_ice101_lockouts()

    def on_vending_capabilities(self, subsystem: str, data: dict, caps) -> None:
        """Capabilities subscriber; same derivation as Availability's."""
        if subsystem == "vending" and caps is not None:
            self.on_bin_instrumented(declares_bin_sensor(caps))

    def on_bin_instrumented(self, instrumented: bool) -> None:
        """The vending board's bin-sensor declaration changed.

        False with an ICE-101 lockout held and no timer pending arms the
        cooldown (the raise already passed while the sensor was assumed).
        True cancels a pending timer: ``bin_half_full`` now clears instead.
        """
        if instrumented:
            self._cancel()
        elif self._timer is None and any(
            c is FaultCode.ICE_101 for c in self._faults.lockouts.values()
        ):
            self._arm()

    def on_fault_change(self, change: FaultChange) -> None:
        if change.code is not FaultCode.ICE_101:
            return
        if change.action == "raised":
            if not self._bin_instrumented():
                self._arm()
        elif not any(c is FaultCode.ICE_101 for c in self._faults.lockouts.values()):
            self._cancel()

    def _arm(self) -> None:
        self._cancel()
        self._timer = self._tasks.schedule(
            self._cooldown_seconds(), self._on_cooldown, label=COOLDOWN_LABEL
        )

    def _cancel(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_cooldown(self) -> None:
        # Drop the handle first: the clear below re-enters on_fault_change
        # from inside this timer's own task, which must not cancel itself.
        self._timer = None
        self._faults.clear_ice101_lockouts(by="cooldown")
