from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from ice_cloud.alerts.dispatcher import AlertDispatcher
from ice_cloud.store import AlertEventRow, SqliteRepository
from tests.cloud.reports import FP
from tests.cloud.sql_helpers import grant_tech, record_alert_events

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class RecordingMailer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next: Exception | None = None

    async def send(self, msg: EmailMessage) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.sent.append(msg)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def event_row(
    kind="raised", key="WTR-104:machine", gate="safety", at=T0, code="WTR-104"
):
    return AlertEventRow(
        fingerprint=FP,
        kind=kind,
        at=at,
        key=key,
        code=code,
        severity="critical",
        gate=gate,
        description="Sump high",
        sku=None,
        product=None,
    )


class Env:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "cloud.db"
        self.repo = SqliteRepository(self.db_path)
        self.owner = self.repo.upsert_account_from_login(
            "sub-o", "owner@example.com", "Olive", T0
        )
        self.tech = self.repo.upsert_account_from_login(
            "sub-t", "tech@example.com", "Tess", T0
        )
        self.repo.register_machine(FP, "a2V5", "Shop", T0)
        self.repo.claim_machine(FP, self.owner.id, T0)
        grant_tech(self.db_path, self.tech.id, FP, self.owner.id, T0)
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
        return record_alert_events(self.db_path, list(rows))

    async def dispatch(self, *rows):
        stored = self.events(*rows)
        await self.disp.dispatch(FP, self.repo.machine(FP), stored)
        return stored

    def deliveries(self, stored):
        out = self.repo.deliveries_for_events(FP, [e.id for e in stored])
        return {d.id: d for ds in out.values() for d in ds}.values()
