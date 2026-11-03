"""Edge consumer of the retained ``payment/state`` document (cutover spec 5.5).

Feeds the per-device availability rows, raises and clears PAY-106 (coin
changer) and PAY-107 (bill validator), announces "Exact change only" on the
flip to true, and hands every state to ``PolicySync``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from ice_common.observers import Subscribers
from ice_contracts.board.payment import DeviceStatusEvent, PaymentState
from ice_contracts.board.vending_machine import FaultCode

if TYPE_CHECKING:
    from ice_edge.faults.availability import Availability
    from ice_edge.faults.fault_service import FaultService
    from ice_edge.payment.mdb.policy import PolicySync

_RAISE = frozenset({"offline", "error", "tilt", "jammed"})
_CLEAR = frozenset({"ready", "disabled", "busy"})
_DEVICE_FAULTS = {"coin": FaultCode.PAY_106, "bill": FaultCode.PAY_107}


class StateConsumer:
    def __init__(
        self,
        *,
        faults: FaultService,
        availability: Callable[[], Availability | None],
        policy: PolicySync,
    ) -> None:
        self._faults = faults
        self._availability = availability
        self._policy = policy
        self._subs: Subscribers[PaymentState] = Subscribers()
        self.state: PaymentState | None = None
        self.state_ts: float = 0.0

    def subscribe_state(self, cb: Callable[[PaymentState], None]) -> Callable[[], None]:
        return self._subs.subscribe(cb)

    async def handle(self, topic: str, data: dict) -> None:
        try:
            state = PaymentState.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                f"MQTT payment state [{topic}] invalid: {exc.error_count()} errors"
            )
            return
        self.state = state
        self.state_ts = time.time()
        avail = self._availability()
        if avail is not None:
            avail.set_payment_state(state)
        for name in _DEVICE_FAULTS:
            dev = getattr(state.devices, name)
            if dev.present:
                self._apply_status(name, dev.status.value)
            else:
                self._clear_if_active(name)
        self._policy.on_state(state)
        self._subs.publish(state)

    def on_device_status(self, evt: DeviceStatusEvent) -> None:
        device = evt.device.value
        if device in _DEVICE_FAULTS:
            self._apply_status(device, evt.status.value)

    def _apply_status(self, device: str, status: str) -> None:
        code = _DEVICE_FAULTS[device]
        if status in _RAISE:
            if not self._faults.has(code):
                self._faults.raise_fault(code)
        elif status in _CLEAR and self._faults.has(code):
            self._faults.clear_fault(code.value, by="auto")

    def _clear_if_active(self, device: str) -> None:
        code = _DEVICE_FAULTS[device]
        if self._faults.has(code):
            self._faults.clear_fault(code.value, by="auto")
