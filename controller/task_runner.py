"""Asyncio task plumbing: fire-and-forget tasks, delayed callbacks, and the
persistent-task set drained (not cancelled) at shutdown.

Extracted from ``controller.vmc.VMC`` following the same pattern as
``controller/fault_registry.py``'s ``FaultRegistry``,
``controller/escrow_ledger.py``'s ``EscrowLedger``,
``controller/refund_protocol.py``'s ``RefundProtocol``,
``controller/session_recovery.py``'s ``SessionRecovery`` and
``controller/maintenance_lease.py``'s ``MaintenanceLease`` (see
``CLAUDE.md``'s "FSM Core" section). ``TaskRunner`` is pure event-loop
bookkeeping with no domain knowledge whatsoever: it does not know about the
FSM, MQTT, refunds, or sessions, only about ``asyncio.Task`` objects.

The VMC keeps a single ``TaskRunner`` instance (``self._tasks``) and exposes
``_fire_and_forget``/``_schedule``/``drain_persistence``/
``cancel_pending_tasks`` as thin delegates so existing call sites and tests
are unaffected; ``cancel_pending_tasks`` additionally performs the VMC's own
domain cancels (dispense/session timeouts, refund deadlines) after this
class's ``cancel_pending()`` clears the generic lists.
"""

from __future__ import annotations

import asyncio

from loguru import logger


class TaskRunner:
    """Holds the event-loop task bookkeeping the VMC used to keep on itself.

    ``loop`` is ``None`` until ``attach()`` is called (mirroring the VMC's
    own ``attach_to_loop``); every method below tolerates that, treating "no
    loop attached" as a no-op (closing the coroutine for ``fire_and_forget``,
    returning ``None`` with a warning for ``schedule``).
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: list[asyncio.Task] = []
        self.persist: list[asyncio.Task] = []

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the running asyncio event loop. Call before scheduling."""
        self.loop = loop

    def fire_and_forget(self, coro, *, persistent: bool = False) -> None:
        """Run a coroutine on the attached loop without awaiting it.

        The task is kept in ``pending`` (so it is not garbage-collected and
        is cancelled on shutdown) and any exception it raises is logged
        rather than silently dropped — these carry alerts and refund
        commands.

        Pass ``persistent=True`` for session/inventory writes that must not
        be cancelled by a graceful shutdown; such tasks are additionally
        tracked in ``persist`` so ``drain_persistence()`` can await them.
        """
        if self.loop is None or self.loop.is_closed():
            coro.close()
            return
        task = self.loop.create_task(coro)
        task.add_done_callback(self._log_task_failure)
        self.pending.append(task)
        self.pending = [t for t in self.pending if not t.done()]
        if persistent:
            self.persist.append(task)
            self.persist = [t for t in self.persist if not t.done()]

    def schedule(
        self, delay_seconds, callback, *, label: str = ""
    ) -> asyncio.Task | None:
        """Schedule a synchronous callback to run after delay_seconds on the event loop.

        ``label`` identifies the timer for tests (VMC public surface
        design, section 2) -- the real runner accepts and ignores it;
        ``tests.fakes.FakeTaskRunner`` records it so a test can fire a
        specific timer by name instead of poking a private task handle.
        """
        if self.loop is None or self.loop.is_closed():
            logger.warning("No event loop attached; cannot schedule callback.")
            return None

        async def _delayed():
            await asyncio.sleep(delay_seconds)
            callback()

        task = self.loop.create_task(_delayed())
        self.pending.append(task)
        # Clean up finished tasks
        self.pending = [t for t in self.pending if not t.done()]
        return task

    async def drain_persistence(self, timeout: float = 3.0) -> None:
        """Await in-flight session/inventory writes so shutdown never cancels them."""
        pending = [t for t in self.persist if not t.done()]
        if not pending:
            return
        done, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.warning(
                f"Shutdown: {len(still)} persistence task(s) still running after {timeout}s"
            )

    def cancel_pending(self) -> None:
        """Cancel every pending task not tracked as persistent, then clear.

        Persistence writes tracked in ``persist`` are never cancelled here
        — they are drained (awaited to completion) by
        ``drain_persistence()`` instead, so a shutdown cannot truncate an
        in-flight session/inventory save.
        """
        for task in self.pending:
            if not task.done() and task not in self.persist:
                task.cancel()
        self.pending.clear()

    @staticmethod
    def _log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Background task {task.get_name()} failed: {exc!r}")
