"""Primary-task lifecycle coordination using only the standard library."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


class _ExitRequest(Exception):
    """Carries a SystemExit/KeyboardInterrupt out of the primary task.

    asyncio re-raises those two straight out of the event loop when a task
    raises them, which would skip every ``finally`` in the caller.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(repr(original))
        self.original = original


async def _shield_exit(primary: Awaitable[T]) -> T:
    try:
        return await primary
    except (SystemExit, KeyboardInterrupt) as exc:
        raise _ExitRequest(exc) from None


async def run_until_primary_exits(
    primary: Awaitable[T], *background: Awaitable[object]
) -> T:
    """Run concurrently until the primary finishes, fails, or is cancelled.

    Cancel and await background tasks before returning the primary's result or
    propagating an exception. Background tasks should handle their own failures
    (for example, with ``task_supervisor.supervise``); they do not end the run.
    Exceptions from background tasks during cleanup do not replace the primary
    task's outcome. A ``SystemExit`` from the primary (uvicorn calls
    ``sys.exit(1)`` on a bind failure) is re-raised only after that cleanup.
    """
    primary_task = asyncio.ensure_future(_shield_exit(primary))
    background_tasks = [asyncio.ensure_future(c) for c in background]
    try:
        return await primary_task
    except _ExitRequest as request:
        # Re-raised here, in the caller's own frame, after the cleanup below.
        raise request.original from None
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
