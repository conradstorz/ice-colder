"""Event-loop test helpers with no application imports (stdlib only), so
simulator tests can use them without reaching edge code. `tests/fakes.py`
re-exports all of them."""

from __future__ import annotations

import asyncio


async def settle(turns: int = 3) -> None:
    """Yield to the loop `turns` times so fire-and-forget tasks with no real I/O run to completion.
    Replaces `await asyncio.sleep(0.01)`-style waits; never waits on the clock."""
    for _ in range(turns):
        await asyncio.sleep(0)


class _AsyncioWithSleep:
    """Stand-in for the `asyncio` module that overrides only `sleep`."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


def patch_module_sleep(module_name: str, sleep):
    """Patch `sleep` as seen by ONE module, leaving `asyncio.sleep` intact.

    `patch("asyncio.sleep", ...)` mutates the shared asyncio module, so it
    would also replace sleep for pytest-asyncio and the loop's own helpers.
    Instead, rebind the module's own `asyncio` global to a proxy that
    delegates everything but `sleep`. Use as a context manager.
    """
    from unittest.mock import patch

    return patch(f"{module_name}.asyncio", new=_AsyncioWithSleep(sleep))


__all__ = ["settle", "patch_module_sleep"]
