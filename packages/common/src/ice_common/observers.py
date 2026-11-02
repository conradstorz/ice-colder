"""A typed, ordered list of subscriber callbacks.

``Subscribers[P]`` is parameterised over the callback's signature so a
publisher and its subscribers agree on argument shapes at type-check time.
Imports stdlib and loguru only, so any layer may use it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, ParamSpec

from loguru import logger

P = ParamSpec("P")


class Subscribers(Generic[P]):
    """Callbacks invoked in subscription order; one failure never stops the rest."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[P, None]] = []

    def subscribe(self, cb: Callable[P, None]) -> Callable[[], None]:
        """Add ``cb``; return an idempotent unsubscribe callable."""
        self._callbacks.append(cb)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            self._callbacks.remove(cb)

        return unsubscribe

    def publish(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Call every subscriber in order; a raising one is logged, not propagated."""
        for cb in list(self._callbacks):
            try:
                cb(*args, **kwargs)
            except Exception:
                logger.exception(f"Subscriber {cb!r} raised")

    def __len__(self) -> int:
        return len(self._callbacks)


class HealthEdge:
    """Failed/ok reporting for a write path that can fail and recover.

    ``failed()`` runs ``on_failed`` after every failure; ``ok()`` runs ``on_ok``
    only when the previous outcome was a failure (the recovery edge, not every
    success). A raising callback is logged, never propagated. Callbacks run on
    the calling thread, outside the internal lock.
    """

    def __init__(
        self,
        on_failed: Callable[[], None] | None,
        on_ok: Callable[[], None] | None,
    ) -> None:
        self._on_failed = on_failed
        self._on_ok = on_ok
        self._failing = False
        self._lock = threading.Lock()

    @property
    def is_failing(self) -> bool:
        return self._failing

    def failed(self) -> None:
        with self._lock:
            self._failing = True
        self._call(self._on_failed)

    def ok(self) -> None:
        with self._lock:
            if not self._failing:
                return
            self._failing = False
        self._call(self._on_ok)

    @staticmethod
    def _call(cb: Callable[[], None] | None) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.exception(f"Health observer {cb!r} raised")
