import asyncio
from dataclasses import replace
from datetime import timedelta

from ice_cloud.alerts.dispatcher import COOLDOWN_SECONDS
from ice_cloud.alerts.sweep import SWEEP_SECONDS, OfflineSweep
from ice_cloud.store import AlertState
from tests.cloud.alerts.helpers import FP, T0, Env, event_row

import pytest

INTERVAL = 60


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


@pytest.fixture
def sweep(env):
    return OfflineSweep(env.repo, env.disp, now=env.clock)


def test_sweep_period_is_sixty_seconds():
    assert SWEEP_SECONDS == 60


async def test_offline_strictly_after_three_intervals(env, sweep):
    _state(env)
    env.clock.advance(3 * INTERVAL)
    assert await sweep.tick() == []
    assert env.mailer.sent == []
    env.clock.advance(1)
    events = await sweep.tick()
    assert [e.kind for e in events] == ["offline"]
    assert events[0].at == env.clock()
    assert events[0].code is None and events[0].gate is None
    assert len(env.mailer.sent) == 2


async def test_offline_only_once(env, sweep):
    _state(env)
    env.clock.advance(3 * INTERVAL + 1)
    assert len(await sweep.tick()) == 1
    env.clock.advance(SWEEP_SECONDS)
    assert await sweep.tick() == []
    state = env.repo.alert_state(FP)
    assert state.offline_since == T0 + timedelta(seconds=3 * INTERVAL + 1)


async def test_unclaimed_never(env, sweep):
    env.repo.register_machine("other-fp", "a2V5", "Other", T0)
    env.repo.put_alert_state(
        replace(_state(env), fingerprint="other-fp"),
    )
    env.repo.delete_alert_state(FP)
    env.clock.advance(10 * INTERVAL)
    assert await sweep.tick() == []
    assert env.mailer.sent == []


async def test_no_state_row_never(env, sweep):
    env.clock.advance(10 * INTERVAL)
    assert await sweep.tick() == []
    assert env.mailer.sent == []


async def test_tick_flushes_pending(env, sweep):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="b"))
    assert {d.status for d in env.deliveries(stored)} == {"pending"}
    env.clock.advance(COOLDOWN_SECONDS)
    _state(env, last_report_at=env.clock())
    await sweep.tick()
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    assert len(env.mailer.sent) == 4


async def test_tick_dispatch_folds_due_rows_into_offline_email(env, sweep):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="b"))
    env.clock.advance(COOLDOWN_SECONDS)
    _state(env, last_report_at=env.clock() - timedelta(seconds=3 * INTERVAL + 1))
    before = len(env.mailer.sent)
    events = await sweep.tick()
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
    flaky = FlakyRepo(env.repo)
    sweep = OfflineSweep(flaky, env.disp, now=env.clock)
    env.clock.advance(3 * INTERVAL + 1)
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


async def test_startup_grace_then_flag(env, sweep):
    _state(env, last_report_at=T0 - timedelta(seconds=10 * INTERVAL))
    assert await sweep.tick() == []
    assert env.mailer.sent == []
    env.clock.advance(3 * INTERVAL)
    assert await sweep.tick() == []
    env.clock.advance(1)
    events = await sweep.tick()
    assert [e.kind for e in events] == ["offline"]


class FailingPutRepo:
    """Delegates to a real repo; put_alert_state raises for one fingerprint."""

    def __init__(self, repo, bad):
        self.repo = repo
        self.bad = bad

    def put_alert_state(self, state):
        if state.fingerprint == self.bad:
            raise RuntimeError("disk full")
        self.repo.put_alert_state(state)

    def __getattr__(self, name):
        return getattr(self.repo, name)


async def test_one_machine_failing_does_not_block_another(env):
    other = "cd" * 32
    env.repo.register_machine(other, "a2V5", "Other", T0)
    env.repo.claim_machine(other, env.owner.id, T0)
    for fp in (FP, other):
        env.repo.put_alert_state(
            AlertState(
                fingerprint=fp,
                faults=(),
                last_report_at=T0,
                report_interval_seconds=INTERVAL,
                offline_since=None,
            )
        )
    sweep = OfflineSweep(FailingPutRepo(env.repo, FP), env.disp, now=env.clock)
    env.clock.advance(3 * INTERVAL + 1)
    events = await sweep.tick()
    assert [e.fingerprint for e in events] == [other]
    assert [m["To"] for m in env.mailer.sent] == ["owner@example.com"]
