"""Refund protocol: the request -> ack -> one retry -> terminal state machine.

Extracted from ``controller.vmc.VMC`` as the third piece carved off the VMC
god object, following the same pattern as ``controller/fault_registry.py``'s
``FaultRegistry`` and ``controller/escrow_ledger.py``'s ``EscrowLedger`` (see
``CLAUDE.md``'s "FSM Core" section). ``RefundProtocol`` owns ``pending``
(the in-flight ``PendingRefund`` dict, insertion-ordered) and the pure
protocol of sending a refund command, arming/cancelling its ack deadline,
and retrying exactly once with the SAME ``request_id`` before giving up --
the subsystems dedupe on ``request_id`` (see the ``command_dispatcher``
paragraph in CLAUDE.md), so a retry must never mint a fresh one.

It knows nothing about MQTT, the FSM, the event recorder, or the session
store: ``publish``/``schedule`` are injected callables (the VMC passes
closures over its own MQTT client and ``_schedule`` timer primitive), and
the terminal side effects -- persisting the session, transaction logs,
event-recorder rows, customer messages, and raising ``PAY-103`` -- are VMC
callbacks (``on_confirmed``/``on_failed``) rather than anything this module
does itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from loguru import logger

from contracts.vending_machine import (
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
)


@dataclass
class PendingRefund:
    request_id: str
    amount: float
    reason: str
    attempts: int = 1
    deadline_task: asyncio.Task | None = None


class RefundProtocol:
    """Holds the refund request/ack/retry state the VMC used to keep on itself.

    ``ack_timeout``/``max_attempts`` are callables, not plain values, read
    at send time rather than snapshotted at construction -- tests (and, in
    principle, an operator) set ``vmc.REFUND_ACK_TIMEOUT``/
    ``REFUND_MAX_ATTEMPTS`` on the live VMC instance after it is built, and
    a retry must see the current value, not the one in effect when this
    protocol was constructed.
    """

    def __init__(
        self,
        *,
        publish: Callable[[PaymentRefundCommand], None],
        schedule: Callable[..., asyncio.Task | None],
        on_confirmed: Callable[[PendingRefund, float], None],
        on_failed: Callable[[PendingRefund, str], None],
        ack_timeout: Callable[[], float],
        max_attempts: Callable[[], int],
    ) -> None:
        self._publish = publish
        self._schedule = schedule
        self._on_confirmed = on_confirmed
        self._on_failed = on_failed
        self._ack_timeout = ack_timeout
        self._max_attempts = max_attempts
        # Insertion-ordered -- first_request_id() below relies on that.
        self.pending: dict[str, PendingRefund] = {}

    def begin(self, amount: float, reason: str) -> PendingRefund:
        """Mint a fresh request_id, track it, and send the first attempt."""
        pending = PendingRefund(request_id=uuid4().hex, amount=amount, reason=reason)
        self.pending[pending.request_id] = pending
        self._send(pending)
        return pending

    def _send(self, pending: PendingRefund) -> None:
        cmd = PaymentRefundCommand(
            request_id=pending.request_id, amount=pending.amount, reason=pending.reason
        )
        self._publish(cmd)
        pending.deadline_task = self._schedule(
            self._ack_timeout(),
            lambda: self._deadline(pending.request_id),
            label="refund_deadline",
        )

    def handle_ack(self, result: PaymentRefundResult) -> None:
        """Dispatch a validated ack to its pending refund, or warn if unknown."""
        pending = self.pending.get(result.request_id)
        if pending is None:
            logger.warning(f"Refund ack for unknown request_id {result.request_id}")
            return
        if result.status is RefundStatus.ok:
            self.confirmed(pending, result.amount_returned)
        else:
            self.attempt_failed(pending, detail=result.detail or result.status.value)

    def _deadline(self, request_id: str) -> None:
        pending = self.pending.get(request_id)
        if pending is None:
            return
        pending.deadline_task = None
        self.attempt_failed(pending, detail="ack_timeout")

    def _cancel_deadline(self, pending: PendingRefund) -> None:
        if pending.deadline_task and not pending.deadline_task.done():
            pending.deadline_task.cancel()
        pending.deadline_task = None

    def confirmed(self, pending: PendingRefund, amount_returned: float) -> None:
        self._cancel_deadline(pending)
        self.pending.pop(pending.request_id, None)
        self._on_confirmed(pending, amount_returned)

    def attempt_failed(self, pending: PendingRefund, detail: str) -> None:
        self._cancel_deadline(pending)
        if pending.attempts < self._max_attempts():
            pending.attempts += 1
            logger.warning(
                f"Refund {pending.request_id} not confirmed ({detail}); "
                f"retry {pending.attempts}/{self._max_attempts()}"
            )
            self._send(pending)
            return
        self.pending.pop(pending.request_id, None)
        self._on_failed(pending, detail)

    def cancel_all(self) -> None:
        """Cancel every live deadline task. Call during shutdown."""
        for pending in self.pending.values():
            if pending.deadline_task and not pending.deadline_task.done():
                pending.deadline_task.cancel()

    def first_request_id(self) -> str | None:
        """The earliest in-flight refund's request_id, or None."""
        return next(iter(self.pending), None)
