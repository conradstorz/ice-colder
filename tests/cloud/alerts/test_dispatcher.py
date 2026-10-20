from datetime import timedelta

import asyncio

import pytest

from ice_cloud.alerts.dispatcher import COOLDOWN_SECONDS, AlertDispatcher
from tests.cloud.alerts.helpers import FP, T0, Env, event_row


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def test_one_email_per_recipient_per_pass(env):
    stored = await env.dispatch(event_row(key="a"), event_row(key="b"))
    assert len(env.mailer.sent) == 2
    assert {m["To"] for m in env.mailer.sent} == {
        "owner@example.com",
        "tech@example.com",
    }
    rows = list(env.deliveries(stored))
    assert len(rows) == 2
    assert all(d.status == "sent" and d.attempts == 1 for d in rows)
    assert all(len(d.event_ids) == 2 for d in rows)


async def test_cleared_rides_along_since_last_sent(env):
    env.events(
        event_row("cleared", key="old", code="ICE-999", at=T0 + timedelta(seconds=1))
    )
    env.clock.advance(5)
    await env.dispatch(event_row(key="new"))
    owner_msg = next(m for m in env.mailer.sent if m["To"] == "owner@example.com")
    body = owner_msg.get_content()
    assert "Cleared since your last email" in body and "ICE-999" in body
    env.clock.advance(COOLDOWN_SECONDS + 1)
    await env.dispatch(event_row(key="newer"))
    later = [m for m in env.mailer.sent if m["To"] == "owner@example.com"][-1]
    assert "ICE-999" not in later.get_content()


async def test_cleared_alone_sends_nothing(env):
    stored = await env.dispatch(event_row("cleared"))
    assert env.mailer.sent == []
    assert list(env.deliveries(stored)) == []


async def test_inside_cooldown_creates_pending_and_flush_sends_after(env):
    await env.dispatch(event_row(key="a"))
    assert len(env.mailer.sent) == 2
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="b"))
    assert len(env.mailer.sent) == 2
    assert {d.status for d in env.deliveries(stored)} == {"pending"}
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == 2
    env.clock.advance(COOLDOWN_SECONDS)
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == 4
    assert {d.status for d in env.deliveries(stored)} == {"sent"}


async def test_failed_send_marks_failed_and_retries_on_flush(env):
    env.mailer.fail_next = RuntimeError("smtp down")
    stored = await env.dispatch(event_row())
    statuses = sorted(d.status for d in env.deliveries(stored))
    assert statuses == ["failed", "sent"]
    failed = next(d for d in env.deliveries(stored) if d.status == "failed")
    assert failed.attempts == 1 and "smtp down" in failed.error
    env.clock.advance(60)
    await env.disp.flush(env.clock())
    assert {d.status for d in env.deliveries(stored)} == {"failed", "sent"}
    env.clock.advance(120)
    await env.disp.flush(env.clock())
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    assert len(env.mailer.sent) == 2


async def test_failed_retries_back_off_then_stop(env):
    class Down:
        async def send(self, msg):
            raise RuntimeError("x" * 900)

    env.disp = AlertDispatcher(
        env.repo,
        Down(),
        now=env.clock,
        public_url=None,
        from_addr="a@example.com",
    )
    stored = await env.dispatch(event_row(key="k", gate="alert"))
    (d,) = env.deliveries(stored)
    assert d.status == "failed" and d.attempts == 1 and len(d.error) <= 500
    for _ in range(5):
        env.clock.advance(1)
        await env.disp.flush(env.clock())
    (d,) = env.deliveries(stored)
    assert d.attempts == 1  # five quick flushes do not exhaust the row
    for minutes in (2, 4, 8, 16):
        env.clock.advance(minutes * 60)
        await env.disp.flush(env.clock())
    (d,) = env.deliveries(stored)
    assert d.attempts == 5
    env.clock.advance(3600)
    await env.disp.flush(env.clock())
    (d,) = env.deliveries(stored)
    assert d.attempts == 5


async def test_muted_recipient_row_status_muted(env):
    env.repo.set_muted(env.tech.id, FP, True)
    stored = await env.dispatch(event_row())
    assert [m["To"] for m in env.mailer.sent] == ["owner@example.com"]
    by_status = {d.account_id: d.status for d in env.deliveries(stored)}
    assert by_status == {env.owner.id: "sent", env.tech.id: "muted"}


async def test_mailer_exception_does_not_propagate(env):
    env.mailer.fail_next = ValueError("boom")
    stored = await env.dispatch(event_row())
    assert sorted(d.status for d in env.deliveries(stored)) == ["failed", "sent"]
    assert len(env.mailer.sent) == 1


async def test_tech_not_emailed_for_alert_gate(env):
    stored = await env.dispatch(event_row(gate="alert"))
    assert [m["To"] for m in env.mailer.sent] == ["owner@example.com"]
    assert [d.account_id for d in env.deliveries(stored)] == [env.owner.id]


async def test_flush_resolves_events_by_id_not_recency(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(event_row(key="held"))
    env.events(*[event_row(key=f"newer{i}", gate="alert") for i in range(5)])
    env.clock.advance(COOLDOWN_SECONDS)
    await env.disp.flush(env.clock())
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    body = env.mailer.sent[-1].get_content()
    assert "newer" not in body and "Raised" in body


async def test_multiple_pending_rows_bundle_into_one_email(env):
    await env.dispatch(event_row(key="first", code="AAA-1"))
    assert len(env.mailer.sent) == 2
    env.clock.advance(60)
    one = await env.dispatch(event_row(key="k1", code="BBB-1"))
    env.clock.advance(60)
    two = await env.dispatch(event_row(key="k2", code="CCC-1"))
    env.clock.advance(60)
    three = await env.dispatch(event_row(key="k3", code="DDD-1"))
    env.clock.advance(600)
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == 4
    body = next(
        m for m in env.mailer.sent[2:] if m["To"] == "owner@example.com"
    ).get_content()
    assert all(c in body for c in ("BBB-1", "CCC-1", "DDD-1"))
    assert {d.status for d in env.deliveries(one + two + three)} == {"sent"}


async def test_failed_and_pending_rows_go_out_together(env):
    env.mailer.fail_next = RuntimeError("smtp down")
    first = await env.dispatch(event_row(key="a", code="AAA-1", gate="alert"))
    assert [d.status for d in env.deliveries(first)] == ["failed"]
    env.clock.advance(1)
    second = await env.dispatch(event_row(key="b", code="BBB-1", gate="alert"))
    assert [d.status for d in env.deliveries(second)] == ["sent"]
    assert [d.status for d in env.deliveries(first)] == ["sent"]
    assert "AAA-1" in env.mailer.sent[-1].get_content()
    env.clock.advance(1)
    third = await env.dispatch(event_row(key="c", code="CCC-1", gate="alert"))
    assert [d.status for d in env.deliveries(third)] == ["pending"]
    env.clock.advance(COOLDOWN_SECONDS)
    before = len(env.mailer.sent)
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == before + 1
    body = env.mailer.sent[-1].get_content()
    assert "CCC-1" in body
    assert {d.status for d in env.deliveries(first + third)} == {"sent"}


async def test_dispatch_and_flush_do_not_double_send(env):
    gate = asyncio.Event()
    entered = asyncio.Event()
    inner = env.mailer

    class Slow:
        async def send(self, msg):
            entered.set()
            await gate.wait()
            await inner.send(msg)

    env.disp = AlertDispatcher(
        env.repo, Slow(), now=env.clock, public_url=None, from_addr="a@example.com"
    )
    env.repo.set_muted(env.tech.id, FP, True)
    stored = env.events(event_row(gate="alert"))
    task = asyncio.create_task(env.disp.dispatch(FP, env.repo.machine(FP), stored))
    await entered.wait()
    flusher = asyncio.create_task(env.disp.flush(env.clock()))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(task, flusher)
    assert len(env.mailer.sent) == 1
    sent = [d for d in env.deliveries(stored) if d.status == "sent"]
    assert [d.attempts for d in sent] == [1]


async def test_cleared_recorded_during_send_rides_next_email(env):
    inner = env.mailer
    env.repo.set_muted(env.tech.id, FP, True)

    class Racing:
        async def send(self, msg):
            env.clock.advance(5)
            env.events(event_row("cleared", key="race", code="RACE-1", at=env.clock()))
            env.clock.advance(5)
            await inner.send(msg)

    env.disp = AlertDispatcher(
        env.repo, Racing(), now=env.clock, public_url=None, from_addr="a@example.com"
    )
    await env.dispatch(event_row(key="a"))
    env.disp = AlertDispatcher(
        env.repo, inner, now=env.clock, public_url=None, from_addr="a@example.com"
    )
    env.clock.advance(COOLDOWN_SECONDS)
    await env.dispatch(event_row(key="b"))
    assert "RACE-1" in env.mailer.sent[-1].get_content()


async def test_dispatch_folds_in_due_rows(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    held = await env.dispatch(event_row(key="b", code="BBB-1"))
    env.clock.advance(COOLDOWN_SECONDS)
    sent_before = len(env.mailer.sent)
    fresh = await env.dispatch(event_row(key="c", code="CCC-1"))
    assert len(env.mailer.sent) == sent_before + 2
    owner_msg = [m for m in env.mailer.sent if m["To"] == "owner@example.com"][-1]
    body = owner_msg.get_content()
    assert "BBB-1" in body and "CCC-1" in body
    assert {d.status for d in env.deliveries(held)} == {"sent"}
    assert {d.status for d in env.deliveries(fresh)} == {"sent"}


async def test_offline_held_then_online_sends_back_online(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    await env.dispatch(event_row("offline", key=None, code=None, gate=None))
    env.clock.advance(60)
    await env.dispatch(event_row("online", key=None, code=None, gate=None))
    env.clock.advance(COOLDOWN_SECONDS)
    before = len(env.mailer.sent)
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == before + 2
    msg = env.mailer.sent[-1]
    assert msg["Subject"].endswith(": back online")
    body = msg.get_content()
    assert "Stopped reporting at" in body and "Reporting again at" in body
    assert body.index("Stopped reporting") < body.index("Reporting again")


async def test_online_held_then_offline_sends_offline(env):
    await env.dispatch(event_row(key="a"))
    env.clock.advance(60)
    await env.dispatch(event_row("online", key=None, code=None, gate=None))
    env.clock.advance(60)
    await env.dispatch(event_row("offline", key=None, code=None, gate=None))
    env.clock.advance(COOLDOWN_SECONDS)
    before = len(env.mailer.sent)
    await env.disp.flush(env.clock())
    assert len(env.mailer.sent) == before + 2
    msg = env.mailer.sent[-1]
    assert msg["Subject"].endswith(": offline")
    body = msg.get_content()
    assert body.index("Reporting again") < body.index("Stopped reporting")
