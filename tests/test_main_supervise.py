"""Tests for main._supervise — the crash-restart wrapper for long-running tasks."""

import asyncio

import pytest

import main as main_mod


async def test_supervise_restarts_after_crash(monkeypatch):
    monkeypatch.setattr(main_mod, "_SUPERVISE_RESTART_DELAY", 0.01)
    calls = {"n": 0}
    ran_again = asyncio.Event()
    block = asyncio.Event()

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        ran_again.set()
        await block.wait()

    task = asyncio.create_task(main_mod._supervise("flaky", flaky))
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

    task = asyncio.create_task(main_mod._supervise("forever", forever))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestRunUntilServerExits:
    """_run_until_server_exits is the process-exit fix: when the server
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

        await main_mod._run_until_server_exits(fake_server(), fake_supervisor())
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
            await main_mod._run_until_server_exits(fake_server(), fake_supervisor())
        assert supervisor_cancelled.is_set()

    async def test_works_with_no_supervised_coroutines(self):
        async def fake_server():
            return "served"

        result = await main_mod._run_until_server_exits(fake_server())
        assert result == "served"
