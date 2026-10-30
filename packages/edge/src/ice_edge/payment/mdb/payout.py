"""Ordered payout / cash_sale queue to the MDB board (cutover spec 5.3, 5.4).

One command is in flight at a time. ``request_id`` is minted at enqueue and
reused on every re-send (the board dedupes on it, firmware rule 5), so no path
here ever pays twice: an unconfirmed payout raises PAY-103 and is dropped, it
is never re-sent under a new id. The one exception is a ``rejected`` ack: the
board did nothing and caches the rejection by ``request_id``, so the item is
re-minted before it is retried.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from loguru import logger

from ice_contracts.board.payment import (
    CashSaleParams,
    PayoutParams,
    PayoutReason,
    PayoutResult,
    PayoutStatus,
)
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.hal.command_dispatcher import CommandTimeout, CompletionTimeout
from ice_edge.payment.ports import PendingPayout

BUSY_RETRY_SECONDS = 2.0
TIMEOUT_RETRY_SECONDS = 5.0


class PayoutProtocol:
    def __init__(
        self,
        *,
        dispatcher: Any,
        policy_acked: Callable[[], bool],
        board_alive: Callable[[], bool],
        tasks: Any,
        faults: Any,
        on_paid: Callable[[PendingPayout, int], None],
        on_changed: Callable[[], None],
        on_no_policy: Callable[[], None] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._policy_acked = policy_acked
        self._board_alive = board_alive
        self._tasks = tasks
        self._faults = faults
        self._on_paid = on_paid
        self._on_changed = on_changed
        self._on_no_policy = on_no_policy
        self._queue: deque[PendingPayout] = deque()
        self._inflight: PendingPayout | None = None
        self._draining = False
        self._retry: Any = None
        self._policy_hold = False  # board refused for want of a policy
        # When the last item left the queue: a retained state older than this
        # still shows the escrow before that payout / cash_sale.
        self.last_done_ts: float | None = None

    # -- public --------------------------------------------------------
    @property
    def idle(self) -> bool:
        return not self._queue and self._inflight is None

    def pending(self) -> list[PendingPayout]:
        head = [self._inflight] if self._inflight is not None else []
        return head + list(self._queue)

    def pay_out(self, cents: int, reason: PayoutReason, sale_id: str | None) -> None:
        # Validate before queueing: a poison item must never enter the queue.
        PayoutParams(cents=cents, reason=PayoutReason(reason), sale_id=sale_id)
        self._enqueue(
            PendingPayout(
                uuid.uuid4().hex, "payout", cents, PayoutReason(reason).value, sale_id
            )
        )

    def cash_sale(self, cents: int, sale_id: str | None) -> None:
        CashSaleParams(cents=cents, sale_id=sale_id)
        self._enqueue(
            PendingPayout(uuid.uuid4().hex, "cash_sale", cents, None, sale_id)
        )

    def seed(self, pending: list[PendingPayout]) -> None:
        """Re-queue payouts from a loaded snapshot.

        Not called at boot while PAY-104 is raised: a snapshot's pending
        payouts are PAY-104 evidence and the operator decides.
        """
        valid: list[PendingPayout] = []
        for item in pending:
            try:
                if item.kind == "payout":
                    PayoutParams(
                        cents=item.cents,
                        reason=PayoutReason(item.reason),
                        sale_id=item.sale_id,
                    )
                else:
                    CashSaleParams(cents=item.cents, sale_id=item.sale_id)
            except ValueError:
                # A poison item would fail every send and wedge the queue.
                logger.error(f"dropping invalid pending {item.kind} {item.request_id}")
                self._pay103(f"invalid_pending:{item.request_id}")
                continue
            valid.append(item)
        self._queue.extendleft(reversed(valid))
        if valid:
            self._on_changed()
            self._start()

    def kick(self) -> None:
        """Resume sending (policy acked, PAY-101 cleared, completion)."""
        self._policy_hold = False
        self._start()

    def _start(self) -> None:
        if (
            self._policy_hold
            or self._draining
            or self._retry is not None
            or not self._queue
        ):
            return
        if not self._can_send():
            return
        self._draining = True
        self._tasks.fire_and_forget(self._drain())

    # -- internals -----------------------------------------------------
    def _can_send(self) -> bool:
        return bool(self._policy_acked() and self._board_alive())

    def _enqueue(self, item: PendingPayout) -> None:
        self._queue.append(item)
        self._on_changed()
        self._start()

    def _arm_retry(self, delay: float) -> None:
        if self._retry is not None:
            return
        self._retry = self._tasks.schedule(delay, self._retry_due, label="payout_retry")

    def _retry_due(self) -> None:
        self._retry = None
        self.kick()

    def _finish(self) -> None:
        self._inflight = None
        self.last_done_ts = time.time()
        self._notify_changed()

    def _notify_changed(self) -> None:
        try:
            self._on_changed()
        except Exception as exc:  # noqa: BLE001 - the queue state is already right
            logger.error(f"payout on_changed failed: {exc!r}")

    def _requeue_head(self, *, remint: bool = False) -> None:
        item = self._inflight
        if item is not None:
            if remint:
                # The firmware caches every ack by request_id, rejections
                # included, so a re-send of a rejected id would replay the
                # rejection forever. A rejection means the board did nothing,
                # so a fresh id cannot pay twice.
                item = replace(item, request_id=uuid.uuid4().hex)
            self._queue.appendleft(item)
            self._inflight = None
            if remint:
                self._notify_changed()  # the snapshot must carry the new id

    def _pay103(self, outcome: str) -> None:
        self._raise(FaultCode.PAY_103, outcome)

    def _pay104(self, outcome: str) -> None:
        self._raise(FaultCode.PAY_104, outcome)

    def _raise(self, code: FaultCode, outcome: str) -> None:
        try:
            self._faults.raise_fault(code, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - never re-send over a side effect
            logger.error(f"could not raise {code.value} ({outcome}): {exc!r}")

    def _paid(self, item: PendingPayout, paid: int) -> None:
        try:
            self._on_paid(item, paid)
        except Exception as exc:  # noqa: BLE001 - money moved; never re-send
            logger.error(f"payout on_paid failed for {item.request_id}: {exc!r}")

    async def _drain(self) -> None:
        try:
            while (
                self._queue
                and self._retry is None
                and not self._policy_hold
                and self._can_send()
            ):
                item = self._queue.popleft()
                self._inflight = item
                try:
                    await self._send(item)
                except Exception as exc:  # noqa: BLE001 - last resort, keep the item
                    logger.error(f"MDB send of {item.request_id} failed: {exc!r}")
                    if self._inflight is item:
                        self._requeue_head()
                        self._arm_retry(TIMEOUT_RETRY_SECONDS)
        finally:
            self._draining = False

    async def _send(self, item: PendingPayout) -> None:
        if item.kind == "payout":
            command = "payout"
            params = PayoutParams(
                cents=item.cents, reason=PayoutReason(item.reason), sale_id=item.sale_id
            ).model_dump(mode="json")
        else:
            command = "cash_sale"
            params = CashSaleParams(cents=item.cents, sale_id=item.sale_id).model_dump(
                mode="json"
            )
        try:
            ack = await self._dispatcher.send_and_await_completion(
                "mdb", command, params, request_id=item.request_id
            )
        except CompletionTimeout:
            logger.error(f"MDB {command} {item.request_id} never reported completion")
            if item.kind == "payout":
                self._pay103("unconfirmed")
            else:
                self._pay104("escrow_mismatch:cash_sale_unconfirmed")
            self._finish()
            return
        except CommandTimeout:
            # Never acked (even after the dispatcher's same-id retry): keep it
            # at the head under the same id and try again shortly.
            logger.warning(f"MDB {command} {item.request_id} not acked; will resend")
            self._requeue_head()
            self._arm_retry(TIMEOUT_RETRY_SECONDS)
            return
        except Exception as exc:  # noqa: BLE001
            # No ack was seen, so the board may not have acted: same id, retry.
            logger.error(f"MDB {command} {item.request_id} send failed: {exc!r}")
            self._requeue_head()
            self._arm_retry(TIMEOUT_RETRY_SECONDS)
            return
        try:
            if item.kind == "payout":
                self._settle_payout(item, ack)
            else:
                self._settle_cash_sale(item, ack)
        except Exception as exc:  # noqa: BLE001 - after an ack: never re-send
            logger.error(f"MDB {command} {item.request_id} settle failed: {exc!r}")
            if self._inflight is item:
                if item.kind == "payout":
                    self._pay103("unconfirmed")
                else:
                    self._pay104("escrow_mismatch:cash_sale_unconfirmed")
                self._finish()

    def _reject(self, item: PendingPayout, ack: Any) -> bool:
        """Handle a rejecting ack that left the command unexecuted."""
        detail = getattr(ack, "detail", None)
        if detail == "busy":
            self._requeue_head(remint=True)
            self._arm_retry(BUSY_RETRY_SECONDS)
            return True
        if detail == "expired":
            # The command's timestamp was stale; the policy is fine and no
            # policy toggle will come, so retry on our own with a fresh id.
            self._requeue_head(remint=True)
            self._arm_retry(BUSY_RETRY_SECONDS)
            return True
        if detail == "no_policy":
            self._requeue_head(remint=True)  # waits for the next policy_acked
            self._policy_hold = True
            if self._on_no_policy is not None:
                try:
                    self._on_no_policy()
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"no_policy hook failed: {exc!r}")
            return True
        return False

    def _settle_payout(self, item: PendingPayout, ack: Any) -> None:
        if ack.status == "rejected":
            if self._reject(item, ack):
                return
            self._pay103(f"failed:{ack.detail}")
            self._finish()
            return
        result = None
        if ack.result:
            try:
                result = PayoutResult.model_validate(ack.result)
            except ValueError:
                result = None
        if result is None:
            if ack.status in ("failed", "unsupported"):
                self._pay103(f"failed:{ack.detail}")
            else:
                self._pay103("unconfirmed")
            self._finish()
            return
        paid = result.paid_cents
        if result.status == PayoutStatus.ok:
            self._paid(item, paid)
        elif result.status == PayoutStatus.partial:
            self._paid(item, paid)
            self._pay103(f"partial:{paid}/{result.requested_cents}")
        else:
            self._pay103(f"failed:{result.detail}")
        self._finish()

    def _settle_cash_sale(self, item: PendingPayout, ack: Any) -> None:
        if ack.status == "rejected" and self._reject(item, ack):
            return
        if ack.status == "ok":
            if ack.detail == "clamped":
                logger.warning(f"cash_sale {item.request_id} clamped by the board")
                self._pay104("escrow_mismatch:cash_sale_clamped")
        else:
            logger.error(f"cash_sale {item.request_id} refused: {ack.detail}")
            self._pay104(f"escrow_mismatch:cash_sale_{ack.status}")
        self._finish()
