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
