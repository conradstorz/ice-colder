import asyncio
from dataclasses import replace
from datetime import timedelta

from ice_cloud.alerts.dispatcher import COOLDOWN_SECONDS
from ice_cloud.alerts.sweep import SWEEP_SECONDS, OfflineSweep
from ice_cloud.store import AlertEventRow, AlertState
from tests.cloud.alerts.test_dispatcher import FP, T0, Env

import pytest

INTERVAL = 60


def event_row(key="WTR-104:machine"):
    return AlertEventRow(
        fingerprint=FP,
        kind="raised",
        at=T0,
        key=key,
        code="WTR-104",
        severity="critical",
        gate="safety",
        description="Sump high",
        sku=None,
        product=None,
    )


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _state(env, *, last_report_at=T0, interval=INTERVAL, offline_since=None):
    state = AlertState(
        fingerprint=FP,
        faults=(),
        last_report_at=last_report_at,
        report_interval_seconds=interval,
        offline_since=offline_since,
    )
    env.repo.put_alert_state(state)
    return state


def _sweep(env):
    return OfflineSweep(env.repo, env.disp, now=env.clock)


def test_sweep_period_is_sixty_seconds():
    assert SWEEP_SECONDS == 60


async def test_offline_strictly_after_three_intervals(env):
    _state(env)
    sweep = _sweep(env)
    env.clock.advance(3 * INTERVAL)
    assert await sweep.tick() == []
    assert env.mailer.sent == []
    env.clock.advance(1)
    events = await sweep.tick()
    assert [e.kind for e in events] == ["offline"]
    assert events[0].at == env.clock()
    assert events[0].code is None and events[0].gate is None
    assert len(env.mailer.sent) == 2


async def test_offline_only_once(env):
    _state(env)
    sweep = _sweep(env)
    env.clock.advance(3 * INTERVAL + 1)
    assert len(await sweep.tick()) == 1
    env.clock.advance(SWEEP_SECONDS)
    assert await sweep.tick() == []
    state = env.repo.alert_state(FP)
    assert state.offline_since == T0 + timedelta(seconds=3 * INTERVAL + 1)


async def test_unclaimed_never(env):
    env.repo.register_machine("other-fp", "a2V5", "Other", T0)
    env.repo.put_alert_state(
        replace(_state(env), fingerprint="other-fp"),
    )
    env.repo.delete_alert_state(FP)
    env.clock.advance(10 * INTERVAL)
    assert await _sweep(env).tick() == []
    assert env.mailer.sent == []


async def test_no_state_row_never(env):
    env.clock.advance(10 * INTERVAL)
    assert await _sweep(env).tick() == []
    assert env.mailer.sent == []


async def test_tick_flushes_pending(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="b"))
    assert {d.status for d in env.deliveries(stored)} == {"pending"}
    env.clock.advance(COOLDOWN_SECONDS)
    _state(env, last_report_at=env.clock())
    await _sweep(env).tick()
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    assert len(env.mailer.sent) == 4


async def test_tick_flushes_before_dispatch(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="b"))
    env.clock.advance(COOLDOWN_SECONDS)
    _state(env, last_report_at=env.clock() - timedelta(seconds=3 * INTERVAL + 1))
    before = len(env.mailer.sent)
    events = await _sweep(env).tick()
    assert [e.kind for e in events] == ["offline"]
    assert len(env.mailer.sent) == before + 2
    body = env.mailer.sent[-1].get_content()
    assert "WTR-104" in body and "stopped reporting" in body.lower()
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    assert {d.status for d in env.deliveries(events)} == {"sent"}


class FlakyRepo:
    """Delegates to a real repo; the first claimed_machines_with_state call raises."""

    def __init__(self, repo):
        self.repo = repo
        self.calls = 0

    def claimed_machines_with_state(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("db locked")
        return self.repo.claimed_machines_with_state()

    def __getattr__(self, name):
        return getattr(self.repo, name)


async def test_run_survives_exception(env):
    _state(env)
    env.clock.advance(3 * INTERVAL + 1)
    flaky = FlakyRepo(env.repo)
    sweep = OfflineSweep(flaky, env.disp, now=env.clock)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await sweep.run(sleep=sleep)
    assert sleeps == [SWEEP_SECONDS, SWEEP_SECONDS]
    assert flaky.calls == 2
    assert env.repo.alert_state(FP).offline_since is not None
    assert len(env.mailer.sent) == 2
