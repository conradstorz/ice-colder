"""A typed, ordered list of subscriber callbacks.

``Subscribers[P]`` is parameterised over the callback's signature so a
publisher and its subscribers agree on argument shapes at type-check time.
Imports stdlib and loguru only, so any layer may use it.
"""

from __future__ import annotations

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
