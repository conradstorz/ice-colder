"""Edge intake of ``payment/event`` from the MDB board (cutover spec 5.1).

Parses the lenient header first so every event can be acked, dedupes on
``event_id`` and ``seq``, detects gaps (PAY-105), dispatches by kind, and acks
only after the deposit snapshot and the cursor are persisted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from ice_contracts.board.payment import (
    EVENT_KINDS,
    DeviceStatusEvent,
    PaymentEventAck,
    PaymentEventEnvelope,
    PaymentEventHeader,
    PayoutResult,
)
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.ledger.payment_cursor import CursorState, PaymentCursorStore

if TYPE_CHECKING:
    from ice_edge.faults.fault_service import FaultService

CASHLESS_KINDS = frozenset(
    {"session_begun", "vend_approved", "vend_denied", "session_ended"}
)
CREDIT_METHODS = {"coin_accepted": "cash_coin", "bill_stacked": "cash_bill"}


class EventIntake:
    def __init__(
        self,
        *,
        cursor: PaymentCursorStore,
        publish_ack: Callable[[PaymentEventAck], None],
        deposit: Callable[[int, str, str], None],
        persist_session: Callable[[], Awaitable[None]],
        on_lever: Callable[[], None],
        on_payout_result: Callable[[str, PayoutResult], None],
        on_device_status: Callable[[DeviceStatusEvent], None],
        journal: Callable[[str, dict], None],
        record_event: Callable[[str, int, dict | None], None],
        faults: FaultService,
        on_cursor_saved: Callable[[bool], None],
    ) -> None:
        self._store = cursor
        self._publish_ack = publish_ack
        self._deposit = deposit
        self._persist_session = persist_session
        self._on_lever = on_lever
        self._on_payout_result = on_payout_result
        self._on_device_status = on_device_status
        self._journal = journal
        self._record_event = record_event
        self._faults = faults
        self._on_cursor_saved = on_cursor_saved
        self._lock = asyncio.Lock()
        self._gaps = 0
        self._unknown_kinds = 0
        self._malformed = 0
        self._last_processed_ts: float | None = None
        self._state: CursorState = cursor.load()
        if self._state.corrupt:
            self._raise_pay105("payment cursor was corrupt at boot")

    def seed(self, snapshot_credit_event_ids: Iterable[str]) -> None:
        """Add event ids held as credits in a loaded session.json (boot)."""
        for event_id in snapshot_credit_event_ids:
            if event_id and event_id not in self._state.recent_ids:
                self._state.recent_ids.append(event_id)

    def caught_up_to(self, boot_id: str) -> int:
        return self._state.seqs.get(boot_id, 0)

    @property
    def last_processed_ts(self) -> float | None:
        return self._last_processed_ts

    # -- PAY-105 -----------------------------------------------------------

    def _raise_pay105(self, why: str) -> None:
        outcome = (
            f"{why}; gaps={self._gaps} unknown_kinds={self._unknown_kinds} "
            f"malformed={self._malformed}"
        )
        if not self._faults.has(FaultCode.PAY_105):
            self._faults.raise_fault(FaultCode.PAY_105, outcome=outcome)

    # -- handler -----------------------------------------------------------

    async def handle(self, topic: str, data: dict) -> None:
        try:
            header = PaymentEventHeader.model_validate(data)
        except ValidationError as exc:
            logger.warning(f"Dropping invalid payment event on {topic}: {exc}")
            return
        async with self._lock:
            await self._handle_locked(header, data)

    async def _handle_locked(self, header: PaymentEventHeader, data: dict) -> None:
        st = self._state
        boot, seq = header.boot_id, header.seq
        known = boot in st.seqs
        seq_dup = known and seq <= st.seqs[boot]
        if seq_dup or header.event_id in st.recent_ids:
            logger.info(f"Duplicate payment event {header.event_id}; re-acking")
            expected = (known and seq == st.seqs[boot] + 1) or (not known and seq == 1)
            if not seq_dup and expected:
                # A seeded id (held as a credit by the crashed session): the
                # event is processed already, so the cursor moves past it or
                # the next event would read as a gap (spurious PAY-105).
                st.advance(boot, seq, header.event_id)
                await self._save_cursor(st)
            self._publish_ack(PaymentEventAck(boot_id=boot, seq=seq))
            return

        if (known and seq > st.seqs[boot] + 1) or (not known and seq > 1):
            # The ack below is cumulative, so after a gap the board drops the
            # missing events; a late lower seq is then discarded as a
            # duplicate (contract: keep processing, tell the operator).
            self._gaps += 1
            self._raise_pay105(f"seq gap on boot {boot} at seq {seq}")

        # A deposit failure propagates here: no advance, no ack, so the
        # board's replay is retried as a fresh event. Every other callback is
        # guarded inside _process.
        credited = self._process(header, data)
        st.advance(boot, seq, header.event_id)
        self._last_processed_ts = time.time()
        if credited:
            try:
                await self._persist_session()
            except Exception as exc:  # noqa: BLE001 - upstream raises DATA-104
                logger.error(f"Session snapshot after deposit failed: {exc!r}")
        await self._save_cursor(st)
        self._publish_ack(PaymentEventAck(boot_id=boot, seq=seq))

    async def _save_cursor(self, st) -> None:
        try:
            await self._store.save_async(st)
        except Exception as exc:  # noqa: BLE001 - ack must still go out
            logger.error(f"Payment cursor save failed: {exc!r}")
            self._on_cursor_saved(False)
        else:
            self._on_cursor_saved(True)

    @staticmethod
    def _safe(fn: Callable, *args) -> None:
        """Run a non-money callback; log and continue if it fails."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Payment event callback failed: {exc!r}")

    def _process(self, header: PaymentEventHeader, data: dict) -> bool:
        """Dispatch one new event. Returns True if a deposit was made."""
        kind = header.event.get("kind")
        if kind not in EVENT_KINDS:
            self._unknown_kinds += 1
            logger.warning(f"Unknown payment event kind {kind!r}; acking")
            self._raise_pay105(f"unknown event kind {kind!r}")
            return False
        try:
            body = PaymentEventEnvelope.model_validate(data).event
        except ValidationError as exc:
            self._malformed += 1
            logger.warning(f"Malformed payment event {header.event_id}: {exc}")
            self._raise_pay105(f"malformed {kind} event")
            return False

        if kind in CREDIT_METHODS:
            method = CREDIT_METHODS[kind]
            self._deposit(body.cents, method, header.event_id)
            self._safe(
                self._record_event,
                "payment",
                body.cents,
                {"method": method, "event_id": header.event_id},
            )
            return True
        if kind == "bill_returned":
            self._safe(
                self._journal,
                "bill_returned",
                {"cents": body.cents, "reason": body.reason.value},
            )
        elif kind == "escrow_return_pressed":
            self._safe(self._on_lever)
        elif kind == "payout_result":
            self._safe(self._on_payout_result, body.request_id, body)
        elif kind == "device_status":
            self._safe(self._on_device_status, body)
        elif kind == "cash_collected":
            self._safe(self._journal, "cash_collected", {"device": body.device.value})
            self._safe(
                self._record_event,
                "cash_collected",
                0,
                {"device": body.device.value},
            )
        elif kind in CASHLESS_KINDS:
            logger.info(f"Ignoring cashless payment event {kind} (no reader)")
        return False
