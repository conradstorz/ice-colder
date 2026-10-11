"""Persist the live customer session so a VMC restart cannot silently lose credit.

The snapshot is evidence for the owner (PAY-104), not state to resume: the
FSM always boots idle. Writes are atomic (tmp + fsync + replace) and run on a
single worker thread so they land in submission order without blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from services.paths import DATA_DIR

SESSION_PATH = DATA_DIR / "session.json"

# How far ahead of the wall clock a loaded saved_at may sit before it is
# treated as corrupt rather than as ordinary clock jitter. A snapshot is
# written and read back on the same machine within the same call, so any
# genuine skew is sub-second; this is deliberately generous (two orders of
# magnitude above that) to absorb coarse or slightly-adjusted system clocks
# without ever accepting a value that is meaningfully "in the future" --
# e.g. a hand-edited or tampered file, or a snapshot from a machine whose
# clock is wrong by minutes or more.
SAVED_AT_FUTURE_SKEW_SECONDS = 5.0


@dataclass
class Credit:
    """One deposit still sitting in escrow, in the raw method it arrived as.

    Defined here rather than in controller/vmc.py because SessionSnapshot
    (below) has to serialise a list of these, and vmc.py already imports this
    module — the reverse import would be a cycle.
    """

    method: str
    amount: float
    ts: float


@dataclass
class SessionSnapshot:
    state: str
    credit_escrow: float
    selected_sku: Optional[str] = None
    dispense_slot: Optional[int] = None
    # The dispense mechanism in flight ("bagged_ice"/"water_fill"), set from
    # VMC._sale_mechanism at the moment _snapshot() is built (plan: dispenser
    # profiles, Task 3). Additive and defaults to None, so a snapshot written
    # before this field existed -- which has no "dispense_mechanism" key at
    # all -- still loads via SessionSnapshot(**raw).
    dispense_mechanism: Optional[str] = None
    dispense_started_at: Optional[float] = None
    pending_refund_request_id: Optional[str] = None
    credits: list[Credit] = field(default_factory=list)
    pending_sale_shares: Optional[dict[str, float]] = None
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
            or self.credit_escrow > 0
            or self.state == "dispensing"
            or self.pending_refund_request_id is not None
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
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(snap), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

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
            if raw_credits is not None:
                raw["credits"] = [Credit(**c) for c in raw_credits]
            # saved_at is money-load-bearing (PAY-104 recovery keys on the
            # exact instant a pending sale's escrow shares were written) so
            # it must never be allowed to silently re-default to "now" via
            # SessionSnapshot's field(default_factory=time.time) -- an
            # absent, non-numeric, non-finite, non-positive, or future value
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
            if saved_at > time.time() + SAVED_AT_FUTURE_SKEW_SECONDS:
                raise ValueError(f"saved_at is in the future: {saved_at!r}")
            return SessionSnapshot(**raw)
        except Exception as e:
            logger.error(f"SessionStore: unreadable {self._path}: {e}")
            return SessionSnapshot(state="unknown", credit_escrow=0.0, error=str(e))

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
