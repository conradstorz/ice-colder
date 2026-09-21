"""Persist the live customer session so a VMC restart cannot silently lose credit.

The snapshot is evidence for the owner (PAY-104), not state to resume: the
FSM always boots idle. Writes are atomic (tmp + fsync + replace) and run on a
single worker thread so they land in submission order without blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from services.paths import DATA_DIR

SESSION_PATH = DATA_DIR / "session.json"


@dataclass
class SessionSnapshot:
    state: str
    credit_escrow: float
    selected_sku: Optional[str] = None
    dispense_slot: Optional[int] = None
    dispense_started_at: Optional[float] = None
    pending_refund_request_id: Optional[str] = None
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
            return SessionSnapshot(**raw)
        except Exception as e:
            logger.error(f"SessionStore: unreadable {self._path}: {e}")
            return SessionSnapshot(state="unknown", credit_escrow=0.0, error=str(e))

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as e:
            logger.error(f"SessionStore: could not remove {self._path}: {e}")

    async def save_async(self, snap: SessionSnapshot) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.save, snap)

    async def clear_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.clear)
