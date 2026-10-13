"""Restart supervision for long-running async components.

Depends only on asyncio and Loguru; importing this module does not start tasks
or configure logging.
"""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger


async def supervise(
    name: str,
    coro_factory: Callable[[], Awaitable[object]],
    *,
    restart_delay: float = 5.0,
) -> None:
    """Restart after a crash or unexpected return, until cancelled.

    The factory must create a fresh awaitable for each attempt. Failures are
    logged; cancellation propagates without restarting. Logging sinks are
    configured by the calling application.
    """
    while True:
        try:
            await coro_factory()
            logger.warning(
                f"{name} exited unexpectedly; restarting in {restart_delay:g}s"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{name} crashed; restarting in {restart_delay:g}s")
        await asyncio.sleep(restart_delay)
