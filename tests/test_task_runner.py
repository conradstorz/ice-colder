"""Unit tests for `controller.task_runner.TaskRunner`, in isolation from
`VMC` -- a real event loop (`asyncio_mode = "auto"` runs `async def` tests
directly), no FSM, no MQTT, no domain knowledge at all.
"""

import asyncio
import warnings

from loguru import logger

from controller.task_runner import TaskRunner


# --- fire_and_forget, no loop attached ---


def test_fire_and_forget_with_no_loop_closes_coroutine_without_warning():
    runner = TaskRunner()

    async def never_runs():
        pass

    coro = never_runs()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        runner.fire_and_forget(coro)
    # The coroutine was closed, not merely dropped -- awaiting it (or letting
    # it get garbage-collected unclosed) is what triggers "coroutine was
    # never awaited"; closing it explicitly avoids that warning entirely.
    assert runner.pending == []


# --- fire_and_forget, loop attached ---


async def test_fire_and_forget_runs_the_coroutine():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    ran = []

    async def work():
        ran.append(True)

    runner.fire_and_forget(work())
    await asyncio.sleep(0)
    assert ran == [True]


async def test_fire_and_forget_logs_a_raising_coroutine_without_propagating():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    seen: list[str] = []
    handle = logger.add(
        lambda m: seen.append(str(m)), level="ERROR", format="{message}"
    )
    try:

        async def boom():
            raise RuntimeError("task exploded")

        runner.fire_and_forget(boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        logger.remove(handle)
    assert any("task exploded" in s for s in seen)


# --- persistent tasks and cancel_pending ---


async def test_persistent_tasks_survive_cancel_pending_non_persistent_do_not():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    started = asyncio.Event()

    async def persistent_work():
        started.set()
        await asyncio.sleep(10)

    async def ordinary_work():
        await asyncio.sleep(10)

    runner.fire_and_forget(persistent_work(), persistent=True)
    runner.fire_and_forget(ordinary_work())
    await started.wait()

    persistent_task = runner.persist[0]
    ordinary_tasks = [t for t in runner.pending if t is not persistent_task]
    assert ordinary_tasks

    runner.cancel_pending()
    await asyncio.sleep(0)

    assert not persistent_task.cancelled()
    assert all(t.cancelled() or t.done() for t in ordinary_tasks)
    assert runner.pending == []

    # Clean up: the persistent task is still running after cancel_pending();
    # cancel it directly so the test doesn't leak a live task.
    persistent_task.cancel()
    await asyncio.sleep(0)


# --- schedule ---


async def test_schedule_fires_callback_after_delay_and_returns_task():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    fired = []

    task = runner.schedule(0.02, lambda: fired.append(True))
    assert task is not None
    # The callback must not fire before the delay elapses.
    assert fired == []
    await asyncio.sleep(0.05)
    assert fired == [True]


async def test_schedule_with_no_loop_returns_none_and_warns():
    runner = TaskRunner()
    seen: list[str] = []
    handle = logger.add(
        lambda m: seen.append(str(m)), level="WARNING", format="{message}"
    )
    try:
        result = runner.schedule(0, lambda: None)
    finally:
        logger.remove(handle)
    assert result is None
    assert any("No event loop attached" in s for s in seen)


# --- drain_persistence ---


async def test_drain_persistence_awaits_in_flight_persistent_tasks():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    finished = []

    async def quick_persist():
        await asyncio.sleep(0)
        finished.append(True)

    runner.fire_and_forget(quick_persist(), persistent=True)
    await runner.drain_persistence(timeout=1.0)
    assert finished == [True]


async def test_drain_persistence_warns_when_tasks_outlive_timeout():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())

    async def slow_persist():
        await asyncio.sleep(10)

    runner.fire_and_forget(slow_persist(), persistent=True)
    seen: list[str] = []
    handle = logger.add(
        lambda m: seen.append(str(m)), level="WARNING", format="{message}"
    )
    try:
        await runner.drain_persistence(timeout=0.01)
    finally:
        logger.remove(handle)
    assert any("still running after" in s for s in seen)

    # Clean up the still-running task so the test doesn't leak it.
    for task in runner.persist:
        if not task.done():
            task.cancel()
    await asyncio.sleep(0)


async def test_drain_persistence_is_a_noop_with_nothing_pending():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    await runner.drain_persistence()  # should not raise or hang


# --- pruning finished tasks ---


async def test_finished_tasks_are_pruned_on_next_call():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())

    async def quick():
        pass

    runner.fire_and_forget(quick())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(t.done() for t in runner.pending)

    # The next fire_and_forget call prunes already-finished tasks from the
    # list, leaving only the freshly created one.
    runner.fire_and_forget(quick())
    await asyncio.sleep(0)
    assert len(runner.pending) == 1
