"""Persist the live customer session so a VMC restart cannot silently lose credit.

The snapshot is evidence for the owner (PAY-104), not state to resume: the
FSM always boots idle. Writes are atomic (tmp + fsync + replace) and run on a
single worker thread so they land in submission order without blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from ice_common.paths import DATA_DIR, atomic_write

SESSION_PATH = DATA_DIR / "session.json"

# How far ahead of the wall clock a loaded saved_at may sit before a warning
# is logged. A future value is accepted, never rejected: a Pi with no RTC can
# boot with its clock behind the last save, and rejecting it would discard the
# PAY-104 recovery details. saved_at is kept as-is (it is the recovery key and
# the idempotent-insert ts; a future value is harmless for both). Genuine
# same-clock jitter is sub-second, so this threshold only separates "ordinary"
# from "worth a log line".
SAVED_AT_FUTURE_SKEW_SECONDS = 5.0


@dataclass
class Credit:
    """One deposit still sitting in escrow, in the raw method it arrived as.

    Defined here rather than in ice_edge/sale/vmc.py because SessionSnapshot
    (below) has to serialise a list of these, and vmc.py already imports this
    module — the reverse import would be a cycle.
    """

    method: str
    cents: int
    ts: float
    # The board's payment event id for a credit that arrived as one; lets a
    # restarted edge dedupe a replayed event it already credited. A file
    # written before this field existed loads with None.
    event_id: Optional[str] = None


@dataclass
class SessionSnapshot:
    state: str
    credit_escrow_cents: int
    selected_sku: Optional[str] = None
    dispense_slot: Optional[int] = None
    # The dispense mechanism in flight ("bagged_ice"/"water_fill"), set from
    # VMC._sale_mechanism at the moment _snapshot() is built (plan: dispenser
    # profiles, Task 3). Additive and defaults to None, so a snapshot written
    # before this field existed -- which has no "dispense_mechanism" key at
    # all -- still loads via SessionSnapshot(**raw).
    dispense_mechanism: Optional[str] = None
    dispense_started_at: Optional[float] = None
    # Queued or in-flight ``payout`` / ``cash_sale`` commands (PendingPayout
    # as dicts): money owed to the customer that the board has not confirmed.
    pending_payouts: list[dict] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    pending_sale_shares: Optional[dict[str, int]] = None
    # True only for TestSaleRunner.run_test_sale's simulated sale (system-tests design
    # §2.3/§6), set from the sale's own self._sale_is_test at the moment
    # VMC._snapshot() is built -- never derived from the maintenance lease,
    # which (per spec §6) is never persisted and so has nothing to consult
    # after a restart. Defaults False so a snapshot written before this
    # field existed -- which has no "is_test" key at all -- loads as a
    # PRODUCTION sale, the fail-safe direction: an old real pending sale
    # must keep raising PAY-104 and stay recoverable, never silently
    # dropped because an absent flag was misread as "test". Consulted at
    # two chokepoints so a crashed test sale can never reach the sales
    # ledger even if a second persistence path is added later: VMC.
    # set_session_store() (boot) skips raising PAY-104 for it at all, and
    # VMC.pending_sale_for_recovery() refuses to surface it even if some
    # future path leaves PAY-104 active anyway.
    is_test: bool = False
    saved_at: float = field(default_factory=time.time)
    error: Optional[str] = None  # set when the file could not be parsed

    def is_open(self) -> bool:
        """True when money or a vend was in flight (or we cannot tell)."""
        return (
            bool(self.error)
            or self.credit_escrow_cents > 0
            or self.state == "dispensing"
            or bool(self.pending_sale_shares)
            or bool(self.pending_payouts)
        )


class SessionStore:
    def __init__(self, path: Path = SESSION_PATH):
        self._path = Path(path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="session-store"
        )

    @property
    def path(self) -> Path:
        return self._path

    def save(self, snap: SessionSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps({**asdict(snap), "units": "cents"}))

    def load(self) -> Optional[SessionSnapshot]:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # asdict() flattened Credit to plain dicts on save; a file written
            # before this field existed has no "credits" key at all, which
            # SessionSnapshot(**raw) already handles via default_factory=list
            # — only rehydrate when the key is actually present.
            raw_credits = raw.get("credits")
            # The "units" marker is still written but no longer interpreted.
            raw.pop("units", None)
            # The previous release serialised the removed refund-id field as
            # null on every save; a null carries no information and is
            # dropped. A non-null id is a real legacy pending refund and is
            # left in place so the unknown-key error below fails safe.
            if "pending_refund_request_id" in raw and (
                raw["pending_refund_request_id"] is None
            ):
                del raw["pending_refund_request_id"]
            if raw_credits is not None:
                raw["credits"] = [Credit(**c) for c in raw_credits]
            # saved_at is money-load-bearing (PAY-104 recovery keys on the
            # exact instant a pending sale's escrow shares were written) so
            # it must never be allowed to silently re-default to "now" via
            # SessionSnapshot's field(default_factory=time.time) -- an
            # absent, non-numeric, non-finite, or non-positive value
            # on disk is rejected here and folds into the same
            # unreadable-snapshot "error" channel used below for a JSON
            # parse failure, rather than inventing a second signalling
            # mechanism. That channel already reports is_open() == True
            # (see SessionSnapshot.error), which is the correct, fail-safe
            # answer for a snapshot we cannot actually trust.
            if "saved_at" not in raw:
                raise ValueError("saved_at missing from session snapshot")
            saved_at = raw["saved_at"]
            if (
                isinstance(saved_at, bool)
                or not isinstance(saved_at, (int, float))
                or not math.isfinite(saved_at)
                or saved_at <= 0
            ):
                raise ValueError(f"saved_at is not a valid timestamp: {saved_at!r}")
            skew = saved_at - time.time()
            if skew > SAVED_AT_FUTURE_SKEW_SECONDS:
                logger.warning(
                    f"SessionStore: saved_at in {self._path} is {skew:.0f}s ahead of "
                    "the clock (clock behind the last save?); keeping it as-is"
                )
            return SessionSnapshot(**raw)
        except Exception as e:
            logger.error(f"SessionStore: unreadable {self._path}: {e}")
            detail = str(e)
            kept = self._preserve_unreadable()
            if kept is not None:
                detail += f"; raw file kept at {kept}"
            return SessionSnapshot(state="unknown", credit_escrow_cents=0, error=detail)

    def _preserve_unreadable(self) -> Optional[Path]:
        """Copy an unreadable snapshot beside the original, once per content.

        An unreadable file may be the only record of a pending sale (for
        example a pre-cents file, which is never converted). Clearing PAY-104
        deletes ``session.json``; this copy is what survives for the owner.
        The name carries a content hash so repeated loads do not pile up
        copies and a different file never overwrites an earlier one.
        """
        try:
            data = self._path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()[:16]
            dest = self._path.with_name(
                f"{self._path.stem}.unreadable.{digest}{self._path.suffix}"
            )
            # An existing copy is trusted only if it is byte-identical: a
            # crash mid-copy must not leave a truncated "preserved" file.
            if not (dest.exists() and dest.read_bytes() == data):
                tmp = dest.with_name(dest.name + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, dest)
            return dest
        except OSError as oe:
            logger.error(f"SessionStore: could not keep a copy of {self._path}: {oe}")
            return None

    def clear(self) -> bool:
        """Remove the evidence file. Returns True only if it is gone afterwards."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as e:
            logger.error(f"SessionStore: could not remove {self._path}: {e}")
        return not self._path.exists()

    async def save_async(self, snap: SessionSnapshot) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.save, snap)

    async def clear_async(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.clear)
