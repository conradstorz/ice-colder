"""Unit tests for `tests.fakes.FakeTaskRunner` -- the fake scheduler VMC
timer tests use instead of a real event-loop delay (VMC public surface
design, section 2; plan: vmc-public-surface, Task 3). No VMC involved here,
just the fake in isolation.
"""

import asyncio

import pytest

from tests.fakes import FakeTask, FakeTaskRunner, ScheduledCall


async def test_schedule_records_a_call_and_returns_a_task():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())

    task = runner.schedule(5.0, lambda: None, label="dispense_timeout")

    assert isinstance(task, FakeTask)
    assert len(runner.scheduled) == 1
    call = runner.scheduled[0]
    assert isinstance(call, ScheduledCall)
    assert call.delay == 5.0
    assert call.label == "dispense_timeout"
    assert call.task is task
    assert task.done() is False


async def test_fire_runs_exactly_one_call_and_retires_it():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    fired: list[str] = []
    runner.schedule(1.0, lambda: fired.append("a"), label="dispense_timeout")
    other_task = runner.schedule(
        2.0, lambda: fired.append("b"), label="session_timeout"
    )

    runner.fire("dispense_timeout")

    assert fired == ["a"]
    # The fired call is retired (no longer live); the other one is untouched.
    assert [c.label for c in runner.scheduled] == ["session_timeout"]
    assert other_task.done() is False


async def test_fire_picks_the_most_recent_live_call_with_that_label():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    fired: list[str] = []
    runner.schedule(1.0, lambda: fired.append("first"), label="standby_sweep")
    runner.schedule(1.0, lambda: fired.append("second"), label="standby_sweep")

    runner.fire("standby_sweep")

    assert fired == ["second"]


async def test_fire_lets_a_callback_rearm_under_the_same_label():
    """`MaintenanceLease.sweep_tick` re-schedules itself under the same
    label from inside its own callback -- firing once must not loop and
    must leave exactly the fresh re-armed call live."""
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    ticks: list[int] = []

    def tick():
        ticks.append(len(ticks))
        if len(ticks) < 2:
            runner.schedule(1.0, tick, label="standby_sweep")

    runner.schedule(1.0, tick, label="standby_sweep")

    runner.fire("standby_sweep")
    assert ticks == [0]
    assert len(runner.scheduled) == 1  # the re-armed call

    runner.fire("standby_sweep")
    assert ticks == [0, 1]
    assert runner.scheduled == []  # tick() didn't re-arm this time


async def test_cancel_on_the_task_removes_it_from_scheduled():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    task = runner.schedule(1.0, lambda: None, label="maintenance_idle")

    task.cancel()

    assert runner.scheduled == []
    assert task.done() is True


async def test_fire_on_unknown_label_raises_naming_the_live_labels():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    runner.schedule(1.0, lambda: None, label="dispense_timeout")
    runner.schedule(1.0, lambda: None, label="session_timeout")

    with pytest.raises(LookupError) as exc_info:
        runner.fire("maintenance_idle")

    message = str(exc_info.value)
    assert "dispense_timeout" in message
    assert "session_timeout" in message


async def test_fire_on_unknown_label_with_nothing_scheduled_raises():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())

    with pytest.raises(LookupError):
        runner.fire("dispense_timeout")


async def test_fire_all_runs_every_live_call_once():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    fired: list[str] = []
    runner.schedule(1.0, lambda: fired.append("a"), label="dispense_timeout")
    runner.schedule(1.0, lambda: fired.append("b"), label="session_timeout")

    runner.fire_all()

    assert sorted(fired) == ["a", "b"]
    assert runner.scheduled == []


async def test_fire_and_forget_runs_the_coroutine():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    ran = []

    async def work():
        ran.append(True)

    runner.fire_and_forget(work())
    await asyncio.sleep(0)

    assert ran == [True]


async def test_fire_and_forget_with_no_loop_closes_coroutine():
    runner = FakeTaskRunner()

    async def never_runs():
        pass

    runner.fire_and_forget(never_runs())

    assert runner.pending == []


async def test_cancel_pending_cancels_live_scheduled_calls_too():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    runner.schedule(1.0, lambda: None, label="dispense_timeout")

    runner.cancel_pending()

    assert runner.scheduled == []
