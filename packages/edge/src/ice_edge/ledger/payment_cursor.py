"""Persistent cursor for the MDB payment event stream (cutover spec 5.1.1).

Holds the last processed ``seq`` for the current and previous board boot, plus
the last 64 ``event_id``s. Lives in its own file because ``session.json`` is
deleted whenever a sale closes. Writes are atomic (tmp + fsync + replace).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from ice_common.paths import DATA_DIR

CURSOR_PATH = DATA_DIR / "payment_cursor.json"
MAX_BOOTS = 2
MAX_RECENT_IDS = 64


def _new_ids() -> deque[str]:
    return deque(maxlen=MAX_RECENT_IDS)


@dataclass
class CursorState:
    seqs: dict[str, int] = field(default_factory=dict)
    recent_ids: deque[str] = field(default_factory=_new_ids)
    corrupt: bool = False

    def advance(self, boot_id: str, seq: int, event_id: str) -> None:
        """Record one processed event, keeping the two first-seen-newest boots.

        Eviction is by first-seen order: a late event for an older boot never
        reorders it, so it cannot cause the real previous boot to be evicted.
        """
        self.seqs[boot_id] = seq  # existing key keeps its position
        while len(self.seqs) > MAX_BOOTS:
            del self.seqs[next(iter(self.seqs))]
        self.recent_ids.append(event_id)


class PaymentCursorStore:
    def __init__(self, path: Path = CURSOR_PATH):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> CursorState:
        if not self._path.exists():
            return CursorState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            seqs = {str(k): int(v) for k, v in raw["seqs"].items()}
            ids = [str(i) for i in raw["recent_ids"]]
        except Exception as exc:  # noqa: BLE001 - any parse failure means corrupt
            aside = self._path.with_name(
                f"{self._path.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(self._path, aside)
            except OSError as move_exc:
                logger.error(f"Could not move corrupt payment cursor aside: {move_exc}")
            logger.error(f"Payment cursor unreadable ({exc!r}); moved to {aside}")
            return CursorState(corrupt=True)
        state = CursorState(seqs=dict(list(seqs.items())[-MAX_BOOTS:]))
        state.recent_ids.extend(ids)
        return state

    def save(self, state: CursorState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seqs": state.seqs, "recent_ids": list(state.recent_ids)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    async def save_async(self, state: CursorState) -> None:
        # Snapshot first: the worker thread must not see later mutations.
        snap = CursorState(dict(state.seqs), deque(state.recent_ids, MAX_RECENT_IDS))
        await asyncio.to_thread(self.save, snap)
