"""Primary-task lifecycle coordination using only the standard library."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def run_until_primary_exits(
    primary: Awaitable[T], *background: Awaitable[object]
) -> T:
    """Run concurrently until the primary finishes, fails, or is cancelled.

    Cancel and await background tasks before returning the primary's result or
    propagating an exception. Background tasks should handle their own failures
    (for example, with ``task_supervisor.supervise``); they do not end the run.
    Exceptions from background tasks during cleanup do not replace the primary
    task's outcome.
    """
    primary_task = asyncio.ensure_future(primary)
    background_tasks = [asyncio.ensure_future(c) for c in background]
    try:
        return await primary_task
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
