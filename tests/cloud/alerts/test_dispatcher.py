from datetime import UTC, datetime, timedelta

import pytest

from ice_cloud.alerts.dispatcher import COOLDOWN_SECONDS, AlertDispatcher
from ice_cloud.alerts.mail import RecordingMailer
from ice_cloud.store import AlertEventRow, SqliteRepository
from tests.cloud.reports import FP

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _row(kind="raised", key="WTR-104:machine", gate="safety", at=T0):
    return AlertEventRow(
        fingerprint=FP,
        kind=kind,
        at=at,
        key=key,
        code="WTR-104",
        severity="critical",
        gate=gate,
        description="Sump high",
        sku=None,
        product=None,
    )


class Env:
    def __init__(self, tmp_path):
        self.repo = SqliteRepository(tmp_path / "cloud.db")
        self.owner = self.repo.upsert_account_from_login(
            "sub-o", "owner@example.com", "Olive", T0
        )
        self.tech = self.repo.upsert_account_from_login(
            "sub-t", "tech@example.com", "Tess", T0
        )
        self.repo.register_machine(FP, "a2V5", "Shop", T0)
        self.repo.claim_machine(FP, self.owner.id, T0)
        self.repo.grant_tech(self.tech.id, FP, self.owner.id, T0)
        self.mailer = RecordingMailer()
        self.clock = Clock()
        self.disp = AlertDispatcher(
            self.repo,
            self.mailer,
            now=self.clock,
            public_url="https://cloud.example",
            from_addr="alerts@example.com",
        )

    def events(self, *rows):
        return self.repo.record_alert_events(list(rows))

    async def dispatch(self, *rows):
        stored = self.events(*rows)
        await self.disp.dispatch(FP, self.repo.machine(FP), stored)
        return stored

    def deliveries(self, stored):
        out = self.repo.deliveries_for_events([e.id for e in stored])
        return {d.id: d for ds in out.values() for d in ds}.values()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def test_one_email_per_recipient_per_pass(env):
    stored = await env.dispatch(_row(key="a"), _row(key="b"))
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
    env.events(_row("cleared", key="old"))
    env.clock.advance(5)
    await env.dispatch(_row(key="new"))
    owner_msg = next(m for m in env.mailer.sent if m["To"] == "owner@example.com")
    body = owner_msg.get_content()
    assert "Cleared since your last email" in body


async def test_cleared_alone_sends_nothing(env):
    stored = await env.dispatch(_row("cleared"))
    assert env.mailer.sent == []
    assert list(env.deliveries(stored)) == []


async def test_inside_cooldown_creates_pending_and_flush_sends_after(env):
    await env.dispatch(_row(key="a"))
    assert len(env.mailer.sent) == 2
    env.clock.advance(60)
    stored = await env.dispatch(_row(key="b"))
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
    stored = await env.dispatch(_row())
    statuses = sorted(d.status for d in env.deliveries(stored))
    assert statuses == ["failed", "sent"]
    failed = next(d for d in env.deliveries(stored) if d.status == "failed")
    assert failed.attempts == 1 and "smtp down" in failed.error
    env.clock.advance(1)
    await env.disp.flush(env.clock())
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    assert len(env.mailer.sent) == 2


async def test_sixth_attempt_not_retried(env):
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
    stored = await env.dispatch(_row(key="k", gate="alert"))
    (d,) = env.deliveries(stored)
    assert d.status == "failed" and d.attempts == 1 and len(d.error) <= 500
    for _ in range(10):
        env.clock.advance(1)
        await env.disp.flush(env.clock())
    (d,) = env.deliveries(stored)
    assert d.attempts == 5


async def test_muted_recipient_row_status_muted(env):
    env.repo.set_muted(env.tech.id, FP, True)
    stored = await env.dispatch(_row())
    assert [m["To"] for m in env.mailer.sent] == ["owner@example.com"]
    by_status = {d.account_id: d.status for d in env.deliveries(stored)}
    assert by_status == {env.owner.id: "sent", env.tech.id: "muted"}


async def test_mailer_exception_does_not_propagate(env):
    env.mailer.fail_next = ValueError("boom")
    stored = await env.dispatch(_row())
    assert "failed" in {d.status for d in env.deliveries(stored)}


async def test_tech_not_emailed_for_alert_gate(env):
    stored = await env.dispatch(_row(gate="alert"))
    assert [m["To"] for m in env.mailer.sent] == ["owner@example.com"]
    assert [d.account_id for d in env.deliveries(stored)] == [env.owner.id]


async def test_flush_resolves_events_by_id_not_recency(env):
    await env.dispatch(_row(key="a"))
    env.clock.advance(60)
    stored = await env.dispatch(_row(key="held"))
    env.events(*[_row(key=f"newer{i}", gate="alert") for i in range(5)])
    env.clock.advance(COOLDOWN_SECONDS)
    await env.disp.flush(env.clock())
    assert {d.status for d in env.deliveries(stored)} == {"sent"}
    body = env.mailer.sent[-1].get_content()
    assert "newer" not in body and "Raised" in body
