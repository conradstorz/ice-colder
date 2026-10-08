"""Contracts for the reusable async supervision and lifecycle helpers."""

import asyncio

import pytest

from services.task_lifecycle import run_until_primary_exits
from services.task_supervisor import supervise


async def test_supervise_restarts_after_crash():
    calls = {"n": 0}
    ran_again = asyncio.Event()
    block = asyncio.Event()

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        ran_again.set()
        await block.wait()

    task = asyncio.create_task(supervise("flaky", flaky, restart_delay=0.01))
    await asyncio.wait_for(ran_again.wait(), timeout=5.0)
    assert calls["n"] == 2  # crashed once, restarted once
    assert not task.done()  # supervisor still alive
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervise_propagates_cancellation():
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise("forever", forever))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervise_restarts_after_normal_return():
    calls = 0
    restarted = asyncio.Event()

    async def component():
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        restarted.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise("component", component, restart_delay=0.01))
    try:
        await asyncio.wait_for(restarted.wait(), timeout=5.0)
        assert calls == 2
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestRunUntilPrimaryExits:
    """The process-exit fix: when the server
    coroutine (uvicorn) returns or raises, the whole run must end and the
    still-running supervised background coroutines must be cancelled —
    otherwise gather() waits forever and Docker's restart policy never
    gets a process exit."""

    async def test_returns_when_server_completes_and_cancels_supervisors(self):
        supervisor_cancelled = asyncio.Event()
        supervisor_started = asyncio.Event()

        async def fake_server():
            await asyncio.sleep(0.01)
            return "served"

        async def fake_supervisor():
            supervisor_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                supervisor_cancelled.set()
                raise

        result = await run_until_primary_exits(fake_server(), fake_supervisor())
        assert result == "served"
        assert supervisor_started.is_set()
        assert supervisor_cancelled.is_set()

    async def test_reraises_server_exception_after_cancelling_supervisors(self):
        supervisor_cancelled = asyncio.Event()

        async def fake_server():
            raise RuntimeError("uvicorn boom")

        async def fake_supervisor():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                supervisor_cancelled.set()
                raise

        with pytest.raises(RuntimeError, match="uvicorn boom"):
            await run_until_primary_exits(fake_server(), fake_supervisor())
        assert supervisor_cancelled.is_set()

    async def test_works_with_no_supervised_coroutines(self):
        async def fake_server():
            return "served"

        result = await run_until_primary_exits(fake_server())
        assert result == "served"

    async def test_caller_cancellation_awaits_all_tasks_cleanup(self):
        primary_started = asyncio.Event()
        background_started = [asyncio.Event(), asyncio.Event()]
        cleaned_up = []

        async def component(name, started):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned_up.append(name)

        task = asyncio.create_task(
            run_until_primary_exits(
                component("primary", primary_started),
                component("first", background_started[0]),
                component("second", background_started[1]),
            )
        )
        try:
            await asyncio.wait_for(primary_started.wait(), timeout=5.0)
            for started in background_started:
                await asyncio.wait_for(started.wait(), timeout=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sorted(cleaned_up) == ["first", "primary", "second"]

    async def test_background_cleanup_failure_does_not_mask_primary_or_skip_cleanup(
        self,
    ):
        background_started = [asyncio.Event(), asyncio.Event()]
        later_cleanup_started = asyncio.Event()
        finish_later_cleanup = asyncio.Event()
        later_cleanup_finished = asyncio.Event()

        async def failing_background():
            background_started[0].set()
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("cleanup failed")

        async def later_background():
            background_started[1].set()
            try:
                await asyncio.Event().wait()
            finally:
                later_cleanup_started.set()
                await finish_later_cleanup.wait()
                later_cleanup_finished.set()

        async def primary():
            await asyncio.gather(*(started.wait() for started in background_started))
            return "served"

        task = asyncio.create_task(
            run_until_primary_exits(primary(), failing_background(), later_background())
        )
        await asyncio.wait_for(later_cleanup_started.wait(), timeout=5.0)
        finish_later_cleanup.set()

        assert await task == "served"
        assert later_cleanup_finished.is_set()
