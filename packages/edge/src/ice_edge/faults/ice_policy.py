"""Ice-availability policy: reacts to the vending board's bin sensor.

Today this is only the bin-sensor clear: ``bin_half_full`` going true means
ice is back, so every ICE-101 lockout is cleared automatically.
"""

from __future__ import annotations

from ice_contracts.board.vending_machine import VendingChannel
from ice_edge.faults.fault_service import FaultService


class IcePolicy:
    def __init__(self, *, faults: FaultService) -> None:
        self._faults = faults

    def on_hardware_io(self, device: str, state: bool) -> None:
        if device == VendingChannel.bin_half_full and state:
            self._faults.clear_ice101_lockouts()
