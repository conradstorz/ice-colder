"""`FakeTaskRunner`: a drop-in for `controller.task_runner.TaskRunner` that
lets a test fire a timer by the label the VMC (or one of its extracted
collaborators, `RefundProtocol`/`MaintenanceLease`) armed it with, instead of
reaching into a private per-timer task handle on the VMC (VMC public
surface design, section 2).

Construct a VMC with ``VMC(config, tasks=FakeTaskRunner())``, attach it to the
running loop exactly as the real runner would be, and then:

- ``vmc.tasks.scheduled`` -- every *live* (not yet fired, not cancelled)
  scheduled call, in the order ``schedule()`` was called.
- ``vmc.tasks.fire("dispense_timeout")`` -- runs the most recently scheduled
  live call with that label, retiring it first (its own callback may
  re-schedule under the same label, as ``MaintenanceLease.sweep_tick`` does,
  without this call retroactively seeing its own re-arm as the one it just
  fired).
- ``vmc.tasks.fire_all()`` -- fires every call live at the moment it is
  called (a snapshot taken once, so a callback that re-arms itself under the
  same label never causes this to loop).

``fire_and_forget`` runs the coroutine on the attached loop via
``loop.create_task`` and tracks it exactly like the real runner (pruning
finished tasks, keeping persistent ones in ``persist`` for
``drain_persistence``) -- only ``schedule()`` differs, recording instead of
actually scheduling via ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from controller.task_runner import TaskRunner


@dataclass
class FakeTask:
    """Stands in for the `asyncio.Task` a real `TaskRunner.schedule()` call
    would return. `done()` is true once the call has been fired (via
    `FakeTaskRunner.fire`/`fire_all`) or cancelled -- whichever comes
    first."""

    cancelled: bool = False
    fired: bool = field(default=False, repr=False)

    def done(self) -> bool:
        return self.cancelled or self.fired

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class ScheduledCall:
    """One `FakeTaskRunner.schedule()` call: what was scheduled, under what
    label, and the `FakeTask` handle it was given back."""

    delay: float
    callback: Callable[[], None]
    label: str
    task: FakeTask


class FakeTaskRunner:
    """Same public surface as `TaskRunner`, plus `scheduled`/`fire`/
    `fire_all` -- see the module docstring."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: list[asyncio.Task] = []
        self.persist: list[asyncio.Task] = []
        self.calls: list[ScheduledCall] = []

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def fire_and_forget(self, coro, *, persistent: bool = False) -> None:
        """Run `coro` to completion on the attached loop, tracked exactly
        like the real runner (see `TaskRunner.fire_and_forget`)."""
        if self.loop is None or self.loop.is_closed():
            coro.close()
            return
        task = self.loop.create_task(coro)
        task.add_done_callback(TaskRunner._log_task_failure)
        self.pending.append(task)
        self.pending = [t for t in self.pending if not t.done()]
        if persistent:
            self.persist.append(task)
            self.persist = [t for t in self.persist if not t.done()]

    def schedule(
        self, delay_seconds: float, callback: Callable[[], None], *, label: str = ""
    ) -> FakeTask:
        """Record the call instead of actually scheduling it; never fires
        on its own -- a test fires it explicitly via `fire`/`fire_all`."""
        task = FakeTask()
        self.calls.append(ScheduledCall(delay_seconds, callback, label, task))
        return task

    async def drain_persistence(self, timeout: float = 3.0) -> None:
        pending = [t for t in self.persist if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=timeout)

    def cancel_pending(self) -> None:
        """Cancel every live fire-and-forget task (persistent ones
        excepted) and every live scheduled call -- mirroring the real
        runner, whose `schedule()`-created tasks live in the same
        `pending` list that `fire_and_forget`-created ones do."""
        for task in self.pending:
            if not task.done() and task not in self.persist:
                task.cancel()
        self.pending.clear()
        for call in self.calls:
            if not call.task.done():
                call.task.cancel()

    @property
    def scheduled(self) -> list[ScheduledCall]:
        """Every scheduled call not yet fired or cancelled, in the order
        `schedule()` was called."""
        return [c for c in self.calls if not c.task.done()]

    def fire(self, label: str) -> None:
        """Run the most recently scheduled *live* call with this label,
        retiring its task first -- so a callback that re-arms itself under
        the same label (e.g. `MaintenanceLease.sweep_tick`) produces a
        fresh, distinct live call rather than being mistaken for the one
        just fired.

        Raises `LookupError` naming the currently-live labels if none
        matches -- most often because the timer was never armed (a
        cancelled or superseded timer, or a defensive branch that never
        runs through the scheduler in production either).
        """
        live = self.scheduled
        matching = [c for c in live if c.label == label]
        if not matching:
            live_labels = sorted({c.label for c in live})
            raise LookupError(
                f"No live scheduled call labeled {label!r}; live labels: {live_labels}"
            )
        call = matching[-1]
        call.task.fired = True
        call.callback()

    def fire_all(self) -> None:
        """Fire every call live at the moment this is called -- a snapshot
        taken once, so a callback that re-arms itself under the same (or
        any other) label is never picked up by this same pass."""
        for call in list(self.scheduled):
            call.task.fired = True
            call.callback()
