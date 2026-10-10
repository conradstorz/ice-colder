"""PAY-104 session recovery: the read-and-decide half of crash recovery.

Extracted from ``controller.vmc.VMC`` as the fourth piece carved off the VMC
god object, following the same pattern as ``controller/fault_registry.py``'s
``FaultRegistry``, ``controller/escrow_ledger.py``'s ``EscrowLedger`` and
``controller/refund_protocol.py``'s ``RefundProtocol`` (see ``CLAUDE.md``'s
"FSM Core" section). ``SessionRecovery`` owns the boot-time three-way
decision over a loaded ``SessionSnapshot`` plus the read-only PAY-104
recovery accessor and its record-once guards (``_recorded_pay104_keys``, now
this class's own state). It knows nothing about MQTT, the FSM, the event
recorder, or the health monitor: ``store``/``product_name``/``pay104_active``
are injected callables, and every side effect of a boot decision other than
``SessionStore.clear()`` itself (the event-recorder row, the availability
gate, raising ``PAY-104``) stays on ``VMC`` via ``_flag_uncertain_session``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from services.session_store import SessionSnapshot, SessionStore

BootDecisionKind = Literal["discard_test", "uncertain", "closed", "none"]


@dataclass(frozen=True)
class BootDecision:
    """Result of :meth:`SessionRecovery.evaluate_at_boot`.

    ``kind`` is one of:
      - ``"none"``: no snapshot was on disk (or no store is attached).
      - ``"discard_test"``: an ``is_test`` snapshot was found and has
        already been cleared -- a test sale risks no real money, so no
        PAY-104 is raised for it.
      - ``"uncertain"``: a real, open snapshot was found. The file is left
        in place; the caller must flag the uncertain session (event
        recorder row, availability gate, raising PAY-104) itself.
      - ``"closed"``: a real, closed snapshot was found and has already
        been cleared.

    ``snapshot`` is the loaded ``SessionSnapshot``, or ``None`` for
    ``"none"``.
    """

    kind: BootDecisionKind
    snapshot: SessionSnapshot | None


class SessionRecovery:
    """Holds the PAY-104 recovery state the VMC used to keep on itself."""

    def __init__(
        self,
        *,
        store: Callable[[], SessionStore | None],
        product_name: Callable[[str | None], str | None],
        pay104_active: Callable[[], bool],
    ) -> None:
        self._store = store
        self._product_name = product_name
        self._pay104_active = pay104_active
        # In-memory record-once guard for PAY-104 recovery (Task 14 review
        # finding 3): keys of pending sales this process has already
        # committed via record_sale, checked (and populated) only when the
        # durable marker (mark_pending_sale_recorded) fails to persist --
        # see reserve_pending_sale/pending_sale_already_recorded below.
        # Lost on restart by design; see those methods' docstrings.
        self._recorded_pay104_keys: set[tuple] = set()

    def evaluate_at_boot(self) -> BootDecision:
        """Load any snapshot left by a previous run and decide its fate.

        A snapshot with ``is_test`` is discarded (never raises PAY-104); an
        open snapshot is flagged uncertain (file left in place, side
        effects left to the caller); any other snapshot is cleared. No
        snapshot at all (or no store attached) is ``"none"``.
        """
        store = self._store()
        if store is None:
            return BootDecision(kind="none", snapshot=None)
        snap = store.load()
        if snap is None:
            return BootDecision(kind="none", snapshot=None)
        if snap.is_test:
            # A crashed run_test_sale left this behind. is_test is read
            # straight off the snapshot (not the lease, which spec §6 never
            # persists) so this is the one chokepoint that keeps a crashed
            # test sale from ever raising PAY-104: PAY-104 means "real
            # money may be unaccounted for and an admin must decide", and a
            # test sale risked no real money, so raising it here would be a
            # false alarm. Nothing recoverable was lost -- the file is
            # simply discarded, same as an ordinary closed session below.
            store.clear()
            return BootDecision(kind="discard_test", snapshot=snap)
        if snap.is_open():
            return BootDecision(kind="uncertain", snapshot=snap)
        store.clear()
        return BootDecision(kind="closed", snapshot=snap)

    def pending_sale_for_recovery(self) -> dict | None:
        """Read-only: the pending sale recorded in the session snapshot, if
        any -- feeds the Health > Faults PAY-104 card's "record this sale"
        / "discard" choice (Task 14).

        Returns ``None`` unless ``PAY-104`` is currently an active machine
        fault *and* the on-disk snapshot carries a non-empty
        ``pending_sale_shares``; a card whose snapshot carries no pending
        sale (or whose fault has already been cleared, including by a
        replayed record/discard) keeps the plain Clear button instead of
        the two recovery actions. Never mutates fault state or the
        snapshot -- the caller decides what to do next.

        The price is the sum of the shares, not a fresh catalog lookup:
        the shares are the money actually taken for this sale, whereas the
        catalog price may have been edited (or the product removed from
        the catalog entirely) since the crash, and the row this recovers
        must record what was actually collected, not today's price. The
        product name is still looked up from the catalog by SKU for
        display, falling back to the SKU itself when the product no
        longer exists (`_product_name` already does this).
        """
        if not self._pay104_active():
            return None
        store = self._store()
        if store is None:
            return None
        try:
            snap = store.load()
        except Exception:
            return None
        if snap is None or not snap.pending_sale_shares:
            return None
        if snap.is_test:
            # Defence in depth: evaluate_at_boot() already keeps a
            # test-flagged snapshot from ever raising PAY-104 at boot, so
            # this branch should be unreachable in practice. It stays here
            # anyway so that a *second* future persistence path (or a
            # PAY-104 raised through some other call site while a stale
            # test-sale snapshot happens to still be on disk) still cannot
            # make a test sale recordable through the Health tab -- the
            # never-pruned `sales` ledger must never see a test sale by any
            # route, not just the one this task happened to find.
            return None
        sku = snap.selected_sku
        if sku is None:
            return None
        return {
            "sku": sku,
            "name": self._product_name(sku),
            "slot": snap.dispense_slot,
            "price": round(sum(snap.pending_sale_shares.values()), 2),
            "methods": dict(snap.pending_sale_shares),
            # Not part of the row this recovers and not shown anywhere --
            # carried only so reserve_pending_sale/pending_sale_already_
            # recorded (Task 14 finding 3) can key the in-memory guard on
            # something that distinguishes this particular pending sale
            # from a later, different one. See those methods' docstrings.
            "saved_at": snap.saved_at,
        }

    def _pay104_sale_key(self, pending: dict) -> tuple:
        """Identify one PAY-104 pending sale for the in-memory
        record-once guard (Task 14 review finding 3).

        Keyed on the SKU, the exact method shares (sorted so dict
        ordering never matters), and the snapshot's `saved_at` --
        `process_payment` sets `saved_at` fresh (`time.time()`, via
        `_snapshot()`) at the moment it wrote the escrow shares that
        became this pending sale. A genuinely different pending sale --
        even the same SKU, even a coincidentally identical share
        breakdown -- was written at a different wall-clock instant and
        so gets a different key; a replay of the SAME sale reads the
        SAME on-disk snapshot (nothing rewrites `saved_at` in place
        between reads) and therefore collapses to the same key.
        """
        return (
            pending["sku"],
            tuple(sorted(pending["methods"].items())),
            pending["saved_at"],
        )

    def pending_sale_already_recorded(self, pending: dict) -> bool:
        """True if `pending` (as returned by `pending_sale_for_recovery`)
        has already been reserved via `reserve_pending_sale` in this
        process (Task 14 review finding 3).

        The durable marker (`mark_pending_sale_recorded`) is supposed to
        be what makes a retry safe, but it can fail for the same
        underlying I/O reason that made `clear_fault`'s snapshot removal
        fail one line earlier -- when it does, `pending_sale_for_
        recovery()` keeps (truthfully, per its own unchanged contract)
        reporting the same sale as pending. This in-memory check is the
        belt to that marker's suspenders: called by the route under the
        same `_pay104_lock` as `pending_sale_for_recovery()`'s own read,
        so the two decisions are made atomically. It closes the gap only
        for this process -- a restart loses `_recorded_pay104_keys`
        entirely, same as any other in-memory state, which is why the
        route must also tell the operator the truth (part (a)) rather
        than rely on this alone.
        """
        return self._pay104_sale_key(pending) in self._recorded_pay104_keys

    def reserve_pending_sale(self, pending: dict) -> None:
        """Record, in memory only, that `pending` has been written via
        record_sale -- see `pending_sale_already_recorded` for why this
        exists and what it does not cover. Never raises: this is a
        best-effort belt-and-suspenders guard, not the source of truth."""
        self._recorded_pay104_keys.add(self._pay104_sale_key(pending))

    def mark_pending_sale_recorded(self) -> bool:
        """Durably mark the on-disk PAY-104 snapshot's pending sale as
        already recorded, without touching fault state (Task 14 finding 2).

        Used only from the record-sale route, only after `record_sale` has
        already succeeded but `clear_fault` then failed to remove the
        snapshot (e.g. the file could not be unlinked) -- PAY-104
        legitimately stays active so the operator still has evidence to
        acknowledge, but the sale itself must never be written a second
        time. Rewriting the snapshot with `pending_sale_shares` cleared
        makes `pending_sale_for_recovery()` return ``None`` on any later
        call (its own contract: only non-``None`` when the shares are
        non-empty), regardless of whether the fault is still active, so a
        follow-up record-sale request finds nothing pending and a
        follow-up faults-list render falls back to the plain Clear button.

        Narrow by design: never clears the fault, never writes a sale,
        never raises -- a failure here (no session store attached, the
        snapshot unreadable, or the rewrite itself failing) is logged and
        reported back as ``False`` rather than propagated, since the
        caller already has a sale recorded and a 500 in flight and must
        not lose either to a secondary I/O problem here.
        """
        store = self._store()
        if store is None:
            return False
        try:
            snap = store.load()
        except Exception as e:
            logger.error(f"PAY-104: could not load snapshot to mark recorded: {e}")
            return False
        if snap is None:
            return False
        snap.pending_sale_shares = None
        try:
            store.save(snap)
        except Exception as e:
            logger.error(f"PAY-104: could not save snapshot marked recorded: {e}")
            return False
        return True
