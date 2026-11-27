"""Session evidence: the lifecycle of ``session.json`` as crash evidence.

One deep module for everything that decides what happens to a snapshot an
earlier run (or a failed sale recording) left behind -- see GLOSSARY.md for
*Session evidence*, *Pending sale*, *Record as sale* and *Discard*.

* the **hold** flag: while held, the live persistence path leaves the file
  alone (``StatusOutputs`` and ``DispenseCycle`` get it as injected callables);
* the **boot decision** over a loaded snapshot (``recover_at_boot``);
* the read-only recovery views (``recovery_detail``, ``pending_sale_for_recovery``);
* the operator actions **Record as sale** and **Discard**, serialised by one
  lock, with their in-memory record-once guard and durable marker;
* requeueing the boot snapshot's unconfirmed payouts once PAY-104 resolves.

PAY-104 raised by the payout/reconcile paths is a plain fault and holds no
evidence; only the boot-uncertain path and ``SaleRecordingFailed`` hold it.
The module knows no MQTT, FSM or web types: the recorder for Record as sale is
passed in by the caller, and every other collaborator is injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Literal

from loguru import logger

from ice_contracts.board.vending_machine import FaultCode
from ice_contracts.money import format_cents
from ice_edge.faults.fault_service import FaultChange, FaultService
from ice_edge.ledger.event_recorder import SALE_MATCH_WINDOW_SECONDS
from ice_edge.ledger.session_store import (
    SessionSnapshot,
    SessionStore,
    UnreadableSnapshot,
)
from ice_edge.payment.ports import Payments, PendingPayout, cash_share

_txn_log = logger.bind(transaction=True)

# Added to the configured dispense timeout to get the sale-match window: the
# live path inserts the sale row after the dispense completes, so the row can
# trail the snapshot's ``saved_at`` by up to the timeout plus the write. With
# the default 120 s timeout this reproduces SALE_MATCH_WINDOW_SECONDS (600 s).
SALE_MATCH_MARGIN_SECONDS = 480.0

BootDecisionKind = Literal["discard_test", "uncertain", "closed", "none"]


@dataclass(frozen=True)
class BootDecision:
    """Result of :meth:`SessionEvidence.recover_at_boot`.

    ``kind``: ``"none"`` (no snapshot / no store); ``"discard_test"`` (an
    ``is_test`` snapshot, already cleared -- a test sale risks no real money);
    ``"uncertain"`` (a real open snapshot, file left in place); ``"closed"``
    (a real closed snapshot, already cleared). ``snapshot`` is ``None`` only
    for ``"none"``.
    """

    kind: BootDecisionKind
    snapshot: SessionSnapshot | UnreadableSnapshot | None


@dataclass(frozen=True)
class RecoveryDetail:
    """What the PAY-104 fault row shows about the snapshot left on disk."""

    sku: str | None
    sale_recorded: bool
    credit_escrow_cents: int
    pending_payouts: list[dict]
    error: str | None
    is_test: bool


class RecordOutcome(Enum):
    """Result of :meth:`SessionEvidence.record_pending_sale`."""

    NOTHING_PENDING = "nothing_pending"  # already handled: a no-op, not an error
    RECORDED = "recorded"  # sale written and PAY-104 cleared
    ALREADY_RESERVED = (
        "already_reserved"  # an earlier request here wrote it; marker never persisted
    )
    NO_RECORDER = "no_recorder"  # no event recorder was supplied
    RECORD_FAILED = "record_failed"  # DATA-101 raised; PAY-104 left active
    CASH_SALE_NOT_QUEUED = "cash_sale_not_queued"  # PAY-103 raised; PAY-104 left active
    CLEAR_FAILED_MARKER_WRITTEN = "clear_failed_marker_written"
    MARKER_UNWRITABLE = "marker_unwritable"  # DATA-101 raised; retry is still safe


class DiscardOutcome(Enum):
    """Result of :meth:`SessionEvidence.discard`."""

    NOT_ACTIVE = "not_active"  # PAY-104 already resolved: a no-op, not an error
    CLEARED = "cleared"
    COULD_NOT_CLEAR = "could_not_clear"


class SessionEvidence:
    """Holds the session-evidence state the Machine used to keep on itself."""

    def __init__(
        self,
        *,
        store: Callable[[], SessionStore | None],
        product_name: Callable[[str | None], str | None],
        faults: FaultService,
        payments: Payments,
        recorder: Callable[[], Any] = lambda: None,
        availability: Callable[[], Any] = lambda: None,
        sale_recorded: Callable[[str, float, dict[str, int], float], bool] = (
            lambda sku, since_ts, methods, window: False
        ),
        match_window_seconds: Callable[[], float] = lambda: SALE_MATCH_WINDOW_SECONDS,
        seed_event_ids: Callable[[Iterable[str]], None] = lambda ids: None,
        seed_payouts: Callable[[list[PendingPayout]], None] = lambda items: None,
    ) -> None:
        """``store``/``recorder``/``availability`` are read at call time.
        ``sale_recorded(sku, since_ts, methods, window)`` says whether the
        live path already committed a snapshot's sale (crash between the
        insert and the idle snapshot clear). ``seed_event_ids`` and
        ``seed_payouts`` hand boot-snapshot contents to the payment gateway.
        """
        self._store = store
        self._product_name = product_name
        self._faults = faults
        self._payments = payments
        self._recorder = recorder
        self._availability = availability
        self._sale_recorded = sale_recorded
        self._match_window = match_window_seconds
        self._seed_event_ids = seed_event_ids
        self._seed_payouts = seed_payouts
        # True while session.json is crash evidence the operator must see (an
        # uncertain boot snapshot, or a sale recorded nowhere durable). A
        # runtime PAY-104 (escrow mismatch, cash_sale outcome) does not set it:
        # persistence continues and clearing never deletes the file.
        self._held = False
        self._logged_already_recorded = False
        # In-memory record-once guard for Record as sale: keys of pending
        # sales this process has already written, consulted only when the
        # durable marker failed to persist. Lost on restart by design; the
        # idempotent ``(ts, sku)`` insert is what makes a retry safe.
        self._recorded_keys: set[tuple] = set()
        # Recovered sales whose cash_sale this process already queued.
        self._recovered_cash_sales: set[tuple] = set()
        # The uncertain boot snapshot, held read-only until PAY-104 resolves.
        self._boot_snapshot: SessionSnapshot | None = None
        # One lock for Record and Discard: the pending read, the record_sale
        # write (a worker thread, so the event loop yields mid-span) and the
        # clear must be one critical section, or a second request finds the
        # fault still active and writes a second row. This serialises only
        # within one process: main runs a single in-process uvicorn server, so
        # that is sufficient -- it would not be with multiple worker processes.
        self._lock = asyncio.Lock()

    # --- the hold flag ---

    @property
    def held(self) -> bool:
        return self._held

    def hold(self) -> None:
        self._held = True

    def release(self) -> None:
        self._held = False

    # --- boot ---

    def recover_at_boot(self) -> BootDecision:
        """Evaluate the snapshot an earlier run left and apply its side effects.

        Call once, right after the session store is attached. Seeds the
        gateway's dedupe with the snapshot's deposit event ids (a deposit that
        reached session.json but not the cursor is replayed by the board);
        ``discard_test`` logs a warning; ``uncertain`` holds the evidence and
        raises PAY-104; a ``closed`` snapshot with a pending sale re-queues its
        cash_sale.
        """
        decision = self._evaluate_at_boot()
        snap = decision.snapshot
        if snap is not None:
            credits = snap.credits if isinstance(snap, SessionSnapshot) else ()
            self._seed_event_ids(c.event_id for c in credits)
        if decision.kind == "discard_test":
            logger.warning(
                "SessionStore: discarding a test-sale snapshot found at "
                f"boot (sku={snap.selected_sku!r}, "
                f"state={snap.state!r}); a test sale risks no "
                "real money, so no PAY-104 is raised."
            )
        elif decision.kind == "uncertain":
            # An unreadable file has no payouts to requeue, so nothing is kept.
            self._boot_snapshot = snap if isinstance(snap, SessionSnapshot) else None
            self._flag_uncertain_session(snap)
        elif decision.kind == "closed" and snap.pending_sale_shares:
            self._recover_closed_cash_sale(snap)
        return decision

    def _evaluate_at_boot(self) -> BootDecision:
        """Load any snapshot left by a previous run and decide its fate.

        An ``is_test`` snapshot is discarded (never raises PAY-104); an open
        snapshot is ``uncertain`` (file left in place, side effects left to
        the caller) unless its sale is already recorded and nothing is left
        over; any other snapshot is cleared.
        """
        store = self._store()
        if store is None:
            return BootDecision(kind="none", snapshot=None)
        snap = store.load()
        if snap is None:
            return BootDecision(kind="none", snapshot=None)
        if isinstance(snap, UnreadableSnapshot):
            # We cannot tell what the file held: always uncertain, file kept.
            return BootDecision(kind="uncertain", snapshot=snap)
        if snap.is_test:
            # is_test is read straight off the snapshot (not the lease, which
            # is never persisted): this and ``pending_sale_for_recovery`` are
            # the chokepoints that keep a test sale from ever raising PAY-104
            # or reaching the never-pruned ``sales`` ledger.
            store.clear()
            return BootDecision(kind="discard_test", snapshot=snap)
        if snap.is_open():
            if self._already_recorded(snap) and self._nothing_left_over(snap):
                logger.info(
                    "sale already recorded; snapshot discarded at boot "
                    f"(sku={snap.selected_sku!r})"
                )
                store.clear()
                return BootDecision(kind="closed", snapshot=snap)
            # Either the sale is not recorded, or it is but credit / a refund
            # / an unreadable file still needs an operator: stay uncertain.
            return BootDecision(kind="uncertain", snapshot=snap)
        store.clear()
        return BootDecision(kind="closed", snapshot=snap)

    @staticmethod
    def _nothing_left_over(snap: SessionSnapshot) -> bool:
        """True when the snapshot holds no money or state an operator must see:
        leftover credit or a pending payout."""
        return snap.credit_escrow_cents <= 0 and not snap.pending_payouts

    def _already_recorded(self, snap: SessionSnapshot) -> bool:
        """True when the snapshot's pending sale already has a `sales` row."""
        if snap.selected_sku is None or not snap.pending_sale_shares:
            return False
        try:
            return bool(
                self._sale_recorded(
                    snap.selected_sku,
                    snap.saved_at,
                    dict(snap.pending_sale_shares),
                    self._match_window(),
                )
            )
        except Exception:
            logger.exception("PAY-104: already-recorded check failed; assuming not")
            return False

    def _flag_uncertain_session(
        self, snap: SessionSnapshot | UnreadableSnapshot
    ) -> None:
        """Side effects of an 'uncertain' boot decision: log, record the event
        row, hold the evidence, close the availability gate, raise PAY-104."""
        if isinstance(snap, UnreadableSnapshot):
            detail = snap.detail
            credit_cents = 0
            # The event row keeps the shape it had when the unreadable case was
            # a placeholder snapshot: state "unknown", nothing in escrow.
            metadata = {
                **asdict(SessionSnapshot(state="unknown", credit_escrow_cents=0)),
                "error": detail,
            }
        else:
            detail = (
                f"state={snap.state} escrow={format_cents(snap.credit_escrow_cents)} "
                f"sku={snap.selected_sku} payouts={len(snap.pending_payouts)}"
            )
            credit_cents = snap.credit_escrow_cents
            metadata = asdict(snap)
        logger.error(f"Transaction uncertain after restart: {detail}")
        _txn_log.error(f"RESTART WITH OPEN SESSION: {detail}")
        recorder = self._recorder()
        if recorder:
            recorder.record(
                "session_uncertain",
                value=credit_cents,
                metadata=metadata,
            )
        self.hold()
        availability = self._availability()
        if availability:
            availability.set_transaction_certain(False)
        self._faults.raise_fault(FaultCode.PAY_104, outcome=detail)

    def _recover_closed_cash_sale(self, snap: SessionSnapshot) -> None:
        """The crash fell after the sales row was written but before the
        sale's cash_sale was queued (a closed snapshot holds no pending
        payouts, so none was). Tell the board the cash share left its escrow,
        queued here -- before edge-start reconciliation can evaluate -- so it
        is not paid back out as an excess. The snapshot carries no event ids,
        so the full cash share is sent; if the board rebooted meanwhile the
        board clamps it and PAY-104 is raised (visible, moves no money)."""
        cents = cash_share(snap.pending_sale_shares)
        if cents <= 0:
            return
        logger.warning(
            f"Recovering the cash_sale of an already-recorded sale at boot: "
            f"{format_cents(cents)} (sku={snap.selected_sku!r})"
        )
        self._payments.cash_sale(cents, None)

    # --- PAY-104 resolved ---

    def on_fault_change(self, change: FaultChange) -> None:
        """Fault-change hook. Whatever path resolved PAY-104 (Record as sale,
        Discard, Clear): re-queue the boot snapshot's unconfirmed payouts and
        cash_sales (same request_ids; the board dedupes). The Machine calls
        this before the reconciler runs, so the queue is non-idle when it
        evaluates and the sale's price is never paid out as an excess."""
        if change.action == "cleared" and change.code is FaultCode.PAY_104:
            self._requeue_boot_payouts()

    def _requeue_boot_payouts(self) -> None:
        snap, self._boot_snapshot = self._boot_snapshot, None
        if snap is None or not snap.pending_payouts:
            return
        items = []
        for d in snap.pending_payouts:
            try:
                items.append(PendingPayout(**d))
            except TypeError:
                logger.error(f"dropping malformed pending payout {d!r}")
                self._faults.raise_fault(
                    FaultCode.PAY_103, outcome=f"invalid_pending:{d.get('request_id')}"
                )
        self._seed_payouts(items)

    # --- read-only views ---

    def recovery_detail(self) -> RecoveryDetail | None:
        """Read-only view of the current snapshot for the PAY-104 fault row.
        ``None`` with no store, no snapshot, an unreadable store, or unless the
        evidence is held (a live sale's snapshot is not recovery evidence)."""
        if not self._held:
            return None
        store = self._store()
        if store is None:
            return None
        try:
            snap = store.load()
        except Exception:
            return None
        if snap is None:
            return None
        if isinstance(snap, UnreadableSnapshot):
            return RecoveryDetail(
                sku=None,
                sale_recorded=False,
                credit_escrow_cents=0,
                pending_payouts=[],
                error=snap.detail,
                is_test=False,
            )
        return RecoveryDetail(
            sku=snap.selected_sku,
            sale_recorded=self._already_recorded(snap),
            credit_escrow_cents=snap.credit_escrow_cents,
            pending_payouts=[dict(p) for p in snap.pending_payouts],
            error=None,
            is_test=snap.is_test,
        )

    def pending_sale_for_recovery(self) -> dict | None:
        """Read-only: the pending sale in the session snapshot, if any -- feeds
        the PAY-104 card's "record this sale" / "discard" choice.

        ``None`` unless the evidence is held (a runtime PAY-104 alone leaves
        the LIVE snapshot on disk, which is not a recoverable sale) *and* the
        on-disk snapshot carries non-empty ``pending_sale_shares``. A test
        snapshot is never offered (defence in depth beside ``_evaluate_at_boot``)
        and neither is a sale that already has a ``sales`` row.

        The price is the sum of the shares -- the money actually taken -- not a
        catalog lookup, since the price may have been edited since the crash.
        The name is looked up by SKU for display only.
        """
        if not self._held:
            return None
        store = self._store()
        if store is None:
            return None
        try:
            snap = store.load()
        except Exception:
            return None
        if snap is None or isinstance(snap, UnreadableSnapshot):
            return None
        if not snap.pending_sale_shares:
            return None
        if snap.is_test:
            return None
        sku = snap.selected_sku
        if sku is None:
            return None
        if self._already_recorded(snap):
            if not self._logged_already_recorded:
                self._logged_already_recorded = True
                logger.info(
                    "PAY-104: pending sale already recorded; not offering "
                    f"recovery (sku={sku!r})"
                )
            return None
        return {
            "sku": sku,
            "name": self._product_name(sku),
            "slot": snap.dispense_slot,
            "price_cents": sum(snap.pending_sale_shares.values()),
            "methods": dict(snap.pending_sale_shares),
            # Not shown anywhere: keys the in-memory record-once guard.
            "saved_at": snap.saved_at,
        }

    def plain_clear_refused(self) -> bool:
        """True when a plain Clear of PAY-104 must be refused: clearing removes
        the evidence file, silently losing the only account of a pending sale.
        Record as sale or Discard must be used instead."""
        return self.pending_sale_for_recovery() is not None

    # --- record-once guard and marker (fine-grained, used by Record) ---

    @staticmethod
    def _sale_key(pending: dict) -> tuple:
        """Identify one pending sale: SKU, the method shares (sorted), and the
        snapshot's ``saved_at`` -- written fresh when the shares were, and
        unchanged across reloads, so a replay collapses to the same key while a
        genuinely different sale (even same SKU and shares) does not."""
        return (
            pending["sku"],
            tuple(sorted(pending["methods"].items())),
            pending["saved_at"],
        )

    def _pending_sale_already_recorded(self, pending: dict) -> bool:
        """True if ``pending`` was already reserved in this process. Closes the
        gap only for this process; a restart loses it, which is why the
        idempotent insert, not this, is what makes a retry safe."""
        return self._sale_key(pending) in self._recorded_keys

    def _reserve_pending_sale(self, pending: dict) -> None:
        """Record, in memory only, that ``pending`` has been written. Never raises."""
        self._recorded_keys.add(self._sale_key(pending))

    def _mark_pending_sale_recorded(self) -> bool:
        """Durably mark the on-disk snapshot's pending sale as already recorded
        (clear ``pending_sale_shares``), without touching fault state.

        Used only after the sale was written but clearing PAY-104 failed: the
        fault legitimately stays active, yet the sale must never be written
        again. Never raises -- a failure (no store, unreadable snapshot, failed
        rewrite) is logged and returned as ``False``.
        """
        store = self._store()
        if store is None:
            return False
        try:
            snap = store.load()
        except Exception as e:
            logger.error(f"PAY-104: could not load snapshot to mark recorded: {e}")
            return False
        if snap is None or isinstance(snap, UnreadableSnapshot):
            return False
        snap.pending_sale_shares = None
        try:
            store.save(snap)
        except Exception as e:
            logger.error(f"PAY-104: could not save snapshot marked recorded: {e}")
            return False
        return True

    def _send_recovered_cash_sale(self, pending: dict) -> bool:
        """After Record as sale: tell the board the pending sale's cash share
        left its escrow (``cash_sale(cash share, None)``). Once per sale per
        process; returns whether a command was queued."""
        key = (pending["sku"], pending["saved_at"])
        cents = cash_share(pending["methods"])
        if cents <= 0 or key in self._recovered_cash_sales:
            return False
        self._payments.cash_sale(cents, None)
        self._recovered_cash_sales.add(key)
        return True

    # --- operator actions ---

    async def record_pending_sale(self, recorder: Any) -> RecordOutcome:
        """Record as sale: write the pending sale, then clear PAY-104.

        Write-then-clear, deliberately: ``record_sale`` is durable and journals
        itself before re-raising, so a failure never loses the row, and PAY-104
        stays active rather than cleared with nothing recorded. Clearing first
        would risk a crash leaving no row and no evidence file.

        Idempotency: the session snapshot is the token; once PAY-104 is
        cleared (by this, Discard, or a plain Clear) a replay finds nothing
        pending (``NOTHING_PENDING``). Everything runs under one lock shared
        with :meth:`discard`.

        A successful write followed by a failed clear reserves the sale in
        memory, then durably marks the snapshot (``_mark_pending_sale_recorded``);
        either way no later request in this process writes it again. Safety
        across restarts comes from the database: the write passes
        ``ts=pending["saved_at"]`` and ``idempotent=True``, so a second attempt
        inserts zero rows because ``(ts, sku)`` is already present.
        """
        async with self._lock:
            pending = self.pending_sale_for_recovery()
            if pending is None:
                return RecordOutcome.NOTHING_PENDING
            if self._pending_sale_already_recorded(pending):
                return RecordOutcome.ALREADY_RESERVED
            if recorder is None:
                return RecordOutcome.NO_RECORDER
            try:
                await asyncio.to_thread(
                    recorder.record_sale,
                    pending["sku"],
                    pending["name"],
                    pending["slot"],
                    pending["price_cents"],
                    pending["methods"],
                    ts=pending["saved_at"],
                    idempotent=True,
                )
            except Exception:
                logger.exception(
                    f"PAY-104 record-sale: record_sale failed for "
                    f"sku={pending['sku']!r}; already journaled as fallback by "
                    "record_sale itself -- leaving PAY-104 active so the "
                    "operator can retry"
                )
                self._faults.raise_fault(
                    FaultCode.DATA_101,
                    outcome=f"sku={pending['sku']} price={format_cents(pending['price_cents'])}",
                )
                return RecordOutcome.RECORD_FAILED
            # Tell the board the sale's cash left its escrow before PAY-104
            # clears (clearing releases the armed reconciliation, which would
            # otherwise pay the price out as leftover). If it cannot be queued,
            # PAY-104 and the evidence stay held: a retry re-runs the
            # idempotent insert and queues the cash_sale.
            try:
                self._send_recovered_cash_sale(pending)
            except Exception:
                logger.exception("PAY-104 record-sale: cash_sale could not be queued")
                self._faults.raise_fault(
                    FaultCode.PAY_103, outcome="cash_sale_not_queued"
                )
                return RecordOutcome.CASH_SALE_NOT_QUEUED
            if not self._faults.clear_fault(FaultCode.PAY_104.value, by="admin"):
                # Reserve before anything else, still inside the lock, so even
                # if the durable marker also fails no later request in this
                # process can find a pending sale again.
                self._reserve_pending_sale(pending)
                if self._mark_pending_sale_recorded():
                    return RecordOutcome.CLEAR_FAILED_MARKER_WRITTEN
                # Surface the storage problem as a fault that outlives the
                # HTTP response; retrying is still safe (idempotent insert).
                self._faults.raise_fault(
                    FaultCode.DATA_101,
                    outcome=(
                        f"sku={pending['sku']} price={format_cents(pending['price_cents'])}; "
                        "PAY-104 recovery marker could not be written"
                    ),
                )
                return RecordOutcome.MARKER_UNWRITABLE
            return RecordOutcome.RECORDED

    async def discard(self) -> DiscardOutcome:
        """Discard: clear PAY-104 (removing the snapshot) without recording a
        sale. Idempotent: if PAY-104 is no longer active this is a no-op. Shares
        the lock with :meth:`record_pending_sale`."""
        async with self._lock:
            if not any(
                f["code"] == FaultCode.PAY_104.value
                for f in self._faults.active_faults()
            ):
                return DiscardOutcome.NOT_ACTIVE
            if not self._faults.clear_fault(FaultCode.PAY_104.value, by="admin"):
                return DiscardOutcome.COULD_NOT_CLEAR
            return DiscardOutcome.CLEARED
