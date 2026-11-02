"""`FakeTaskRunner`: a drop-in for `ice_edge.supervisor.task_runner.TaskRunner` that
lets a test fire a timer by the label the VMC (or one of its extracted
collaborators, `MaintenanceLease`) armed it with, instead of
reaching into a private per-timer task handle on the VMC (VMC public
surface design, section 2).

Also holds the small sink fakes shared by `tests/test_outputs.py` and
`tests/test_fault_service.py` (`FakeMqtt`, `FakeStore`, `FakeHealth`,
`FakeAvailability`) -- each records what it was given so a test asserts on
state, not call counts.

Construct a `Machine` with ``Machine(config, tasks=FakeTaskRunner())``
(vmc-reduction plan, Task 6 -- `Machine` is the only construction path),
attach it to the running loop exactly as the real runner would be, and then:

- ``machine.tasks.scheduled`` -- every *live* (not yet fired, not cancelled)
  scheduled call, in the order ``schedule()`` was called.
- ``machine.tasks.fire("dispense_timeout")`` -- runs the most recently
  scheduled live call with that label, retiring it first (its own callback
  may re-schedule under the same label, as ``MaintenanceLease.sweep_tick``
  does, without this call retroactively seeing its own re-arm as the one it
  just fired).
- ``machine.tasks.fire_all()`` -- fires every call live at the moment it is
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
from pathlib import Path

from ice_common.config_model import ConfigModel, PhysicalDetails, Product
from ice_edge.app.machine import Machine
from ice_edge.supervisor.task_runner import TaskRunner
from tests.async_helpers import patch_module_sleep, settle  # noqa: F401  (re-exported)
from tests.dispenser_fixtures import FakeDispatcher, profiles_for
from ice_edge.ledger.session_store import SessionSnapshot
from ice_edge.payment.ports import PendingPayout


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
            if call.task.cancelled or call.task.fired:
                # An earlier callback in this pass cancelled (or fired)
                # it; the real runner would never run it either.
                continue
            call.task.fired = True
            call.callback()


async def drain_pending(runner: FakeTaskRunner) -> None:
    """Await every fire-and-forget task `runner` has launched (including any
    they spawn in turn) until none is live. Use before a NEGATIVE assertion
    ("no refund published", "no vend_failed row") so it cannot pass merely
    because the work has not run yet -- a single `sleep(0)` only proves the
    work got one loop turn. Never use on a task that is gated on something the
    test has not released yet: it would wait for it forever."""
    while True:
        live = [t for t in runner.pending if not t.done()]
        if not live:
            return
        await asyncio.gather(*live, return_exceptions=True)


class FakePayments:
    """A `Payments` that records what the sale FSM asked of the MDB board.

    ``payouts`` holds ``(cents, reason, sale_id)`` and ``cash_sales`` holds
    ``(cents, sale_id)``; ``pending_payouts()`` reports every request not yet
    ``complete()``d, as the real queue does.
    """

    def __init__(self) -> None:
        self.payouts: list[tuple[int, str, str | None]] = []
        self.cash_sales: list[tuple[int, str | None]] = []
        self._pending: list[PendingPayout] = []
        # The board's current boot id; None = unknown (all cash counts).
        self.boot_id: str | None = None

    def board_boot_id(self) -> str | None:
        return self.boot_id

    def pay_out(self, cents, reason, sale_id) -> None:
        value = getattr(reason, "value", reason)
        self.payouts.append((cents, value, sale_id))
        self._pending.append(
            PendingPayout(f"p{len(self.payouts):07d}", "payout", cents, value, sale_id)
        )

    def cash_sale(self, cents, sale_id) -> None:
        self.cash_sales.append((cents, sale_id))
        self._pending.append(
            PendingPayout(
                f"c{len(self.cash_sales):07d}", "cash_sale", cents, None, sale_id
            )
        )

    def pending_payouts(self) -> list[PendingPayout]:
        return list(self._pending)

    def complete(self) -> None:
        self._pending.clear()


class FakeMqtt:
    """Stands in for `ice_common.mqtt_client.MQTTClient`.

    `publish()` records `(topic, payload, retain)` in `published`, and is
    silently dropped while `connected` is false (as the real client does).
    `register()` stores the handler by topic suffix (also in `registered`, in
    call order); `deliver()` simulates an inbound message by calling it, and
    `deliver_ack()`/`deliver_dispenser_report()` do so for the two topics
    `CommandDispatcher.__init__` registers (`cmd/+/ack`, `hardware/dispenser`).
    """

    def __init__(self, connected: bool = True):
        self.connected = connected
        self.published: list[tuple[str, object, bool]] = []
        self.registered: list[tuple[str, object]] = []
        self.handlers: dict[str, Callable] = {}

    def register(self, topic, handler=None, *a, **k):
        self.registered.append((topic, handler))
        self.handlers[topic] = handler

    async def publish(self, topic, payload, qos=1, retain=False):
        if not self.connected:
            return
        self.published.append((topic, payload, retain))

    async def deliver(self, topic, payload, *, registered_as: str | None = None):
        handler = self.handlers[registered_as or topic]
        await handler(topic, payload)

    async def deliver_ack(self, subsystem: str, payload: dict) -> None:
        await self.deliver(f"cmd/{subsystem}/ack", payload, registered_as="cmd/+/ack")

    async def deliver_dispenser_report(self, payload: dict) -> None:
        await self.deliver("hardware/dispenser", payload)


class FakeEventRecorder:
    """Stands in for the event recorder: records every `record`/`record_sale`
    call in `events`/`sales`; `fail_with`, when set, is raised by
    `record_sale` instead of recording it."""

    def __init__(self, fail_with: Exception | None = None):
        self.events: list[tuple] = []
        self.sales: list[tuple] = []
        self.fail_with = fail_with

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.sales.append((sku, name, slot, price, methods))


class FakeFaults:
    """Stands in for `FaultService` for the payment collaborators: `raised`
    holds `(code, outcome)`, `cleared` the keys cleared, `calls` the ordered
    `("raise", code)` / `("clear", key, by)` log; `active` backs `has()`."""

    def __init__(self):
        self.active: set = set()
        self.raised: list[tuple] = []
        self.cleared: list[str] = []
        self.calls: list[tuple] = []

    def has(self, code) -> bool:
        return code in self.active

    def raise_fault(self, code, *, sku=None, outcome=None) -> None:
        self.calls.append(("raise", code))
        self.active.add(code)
        self.raised.append((code, outcome))

    def clear_fault(self, key, *, by="admin") -> bool:
        self.calls.append(("clear", key, by))
        self.cleared.append(key)
        self.active = {c for c in self.active if getattr(c, "value", c) != key}
        return True

    def ensure_raised(self, code, *, outcome=None) -> bool:
        if self.has(code):
            return False
        self.raise_fault(code, outcome=outcome)
        return True

    def ensure_cleared(self, code, *, by="auto") -> bool:
        if not self.has(code):
            return False
        return self.clear_fault(code.value, by=by)


class FakeStore:
    def __init__(self, *, clear_result: bool = True):
        self.saved: list[SessionSnapshot] = []
        self.cleared_async_calls = 0
        self.clear_calls = 0
        self.clear_result = clear_result

    async def save_async(self, snap: SessionSnapshot) -> None:
        self.saved.append(snap)

    async def clear_async(self) -> bool:
        self.cleared_async_calls += 1
        return self.clear_result

    def clear(self) -> bool:
        self.clear_calls += 1
        return self.clear_result


class FakeHealth:
    """Records state pushes, active-fault snapshots, and raised/cleared
    alerts -- the surface `StatusOutputs.state_changed` and
    `FaultService.raise_fault`/`clear_fault`/`push_active_faults` touch."""

    def __init__(self):
        self.states: list[str] = []
        self.active_faults_calls: list[list[dict]] = []
        self.raised_alerts: list[tuple] = []
        self.cleared_alerts: list[str] = []

    def update_vmc_state(self, state: str) -> None:
        self.states.append(state)

    def set_active_faults(self, faults: list[dict]) -> None:
        self.active_faults_calls.append(faults)

    async def raise_alert(
        self,
        key: str,
        level: str,
        source: str,
        message: str,
        code: str | None = None,
        product_sku: str | None = None,
    ) -> None:
        self.raised_alerts.append((key, level, source, message, code, product_sku))

    def clear_alert(self, key: str) -> None:
        self.cleared_alerts.append(key)


class FakeAvailability:
    """Records every call `StatusOutputs.state_changed` and
    `FaultService` make against the real `Availability` surface."""

    def __init__(self):
        self.states: list[str] = []
        self.active_faults_calls: list[list[dict]] = []
        self.subsystem_alive: list[tuple[str, bool]] = []
        self.mqtt_connected: list[bool] = []
        self.republish_calls = 0
        self.transaction_certain: list[bool] = []
        self.publisher = None
        self.hardware_io: list[tuple[str, bool]] = []
        self.capabilities: list[tuple] = []

    def set_hardware_io(self, device: str, state: bool) -> None:
        self.hardware_io.append((device, state))

    def on_vending_capabilities(self, subsystem, data, caps) -> None:
        self.capabilities.append((subsystem, data, caps))

    def set_publisher(self, publisher) -> None:
        self.publisher = publisher

    def set_fsm_state(self, state: str) -> None:
        self.states.append(state)

    def set_active_faults(self, faults: list[dict]) -> None:
        self.active_faults_calls.append(faults)

    def set_subsystem_alive(self, subsystem: str, alive: bool) -> None:
        self.subsystem_alive.append((subsystem, alive))

    def set_mqtt_connected(self, connected: bool) -> None:
        self.mqtt_connected.append(connected)

    def republish(self) -> None:
        self.republish_calls += 1

    def set_transaction_certain(self, certain: bool) -> None:
        self.transaction_certain.append(certain)


def vmc_with_profiles(
    tmp_path: Path,
    products: tuple[Product, ...] | list[Product] = (),
    *,
    tasks: TaskRunner | None = None,
):
    """A `Machine`/`VMC` wired for a real dispatch: *products* in the
    config, loaded dispenser profiles written under *tmp_path*, and a
    `FakeDispatcher` attached -- the minimum wiring a production sale needs
    to actually dispatch. Pass `tasks` (e.g. `FakeTaskRunner()`) for a test
    that fires a timer by label instead of waiting on a real one. Returns
    `(machine, vmc, dispatcher)`; must be called with a running loop."""
    cfg = ConfigModel(physical=PhysicalDetails(products=list(products)))
    machine = Machine(config=cfg, tasks=tasks)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    profiles = profiles_for(list(products), tmp_path)
    machine.set_dispenser_profiles(profiles)
    dispatcher = FakeDispatcher()
    machine.set_command_dispatcher(dispatcher)
    return machine, vmc, dispatcher
