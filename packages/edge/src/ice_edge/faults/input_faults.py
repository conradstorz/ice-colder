"""Water faults derived from the vending board's ``hardware/io`` inputs.

State faults come straight from ``INPUT_FAULTS`` (sump high, low water
pressure, empty RO tank). ``WTR-107`` is a duration rule: ``ro_production``
held true longer than ``ro_max_run_seconds()`` raises it. Pump-off does not
clear it (the board's own cutoff stops the pump, which confirms an overrun);
only ``ro_tank_full`` going true does. All four codes are machine-scope and
never lock a product.
"""

from __future__ import annotations

from collections.abc import Callable

from ice_contracts.board.vending_machine import INPUT_FAULTS, FaultCode, VendingChannel
from ice_edge.faults.fault_service import FaultService
from ice_edge.supervisor.task_runner import TaskRunner, Timer

RO_OVERRUN_LABEL = "ro_overrun"


class InputFaults:
    def __init__(
        self,
        *,
        faults: FaultService,
        tasks: TaskRunner,
        ro_max_run_seconds: Callable[[], float],
    ) -> None:
        self._faults = faults
        self._ro_max_run_seconds = ro_max_run_seconds
        self._by_channel = {f.channel: f for f in INPUT_FAULTS}
        self._overrun_timer = Timer(tasks, RO_OVERRUN_LABEL)

    def on_hardware_io(self, device: str, state: bool) -> None:
        entry = self._by_channel.get(device)
        if entry is not None:
            if state == entry.faulted_when:
                self._raise(entry.code)
            else:
                self._clear(entry.code)
        elif device == VendingChannel.ro_production:
            if state:
                self._arm()
            else:
                self.cancel()
        elif device == VendingChannel.ro_tank_full and state:
            self._clear(FaultCode.WTR_107)

    def cancel(self) -> None:
        """Drop the ``ro_overrun`` timer, if one is pending."""
        self._overrun_timer.cancel()

    def _arm(self) -> None:
        self._overrun_timer.arm(self._ro_max_run_seconds(), self._overrun)

    def _overrun(self) -> None:
        self._raise(FaultCode.WTR_107)

    def _raise(self, code: FaultCode) -> None:
        self._faults.ensure_raised(code)

    def _clear(self, code: FaultCode) -> None:
        self._faults.ensure_cleared(code, by="hardware")
