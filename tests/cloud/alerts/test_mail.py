from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ice_cloud.alerts.mail import (
    LogOnlyMailer,
    RecordingMailer,
    SmtpConfig,
    SmtpMailer,
    make_mailer,
    render_email,
)

AT = datetime(2026, 10, 19, 14, 2, tzinfo=UTC)
LAST = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Ev:
    kind: str
    at: datetime = AT
    code: str | None = None
    severity: str | None = None
    scope: str | None = None
    description: str | None = None
    sku: str | None = None
    product: str | None = None


RAISED = [
    Ev(
        "raised",
        code="WTR-104",
        severity="critical",
        scope="machine",
        description="Sump high-level float tripped",
    ),
    Ev(
        "raised",
        code="CFG-101",
        severity="product_unavailable",
        scope="product",
        description="No valid dispenser profile",
        sku="ICE",
        product="Ice",
    ),
]
CLEARED = [Ev("cleared", code="COM-101", description="Vending board heartbeat lost")]
SHORT = "abcd-efgh-ijkl-mnop"


def build(triggering, cleared, **kw):
    args = dict(
        label="Lobby",
        short=SHORT,
        role="owner",
        triggering=triggering,
        cleared=cleared,
        public_url="https://cloud.example",
        from_addr="alerts@example.com",
        to="o@example.com",
        last_report_at=LAST,
    )
    args.update(kw)
    return render_email(**args)


def test_golden_raised_and_cleared():
    msg = build(RAISED, CLEARED)
    assert (
        msg["Subject"]
        == "[ice-colder] Lobby (abcd-efgh-ijkl-mnop): 2 raised, 1 cleared"
    )
    assert msg["From"] == "alerts@example.com"
    assert msg["To"] == "o@example.com"
    assert msg.get_content() == (
        "Machine Lobby (abcd-efgh-ijkl-mnop)\n"
        "Fault changes at 2026-10-19 14:02 UTC\n"
        "\n"
        "Raised\n"
        "  WTR-104  critical  machine  Sump high-level float tripped\n"
        "  CFG-101  product_unavailable  product Ice (sku ICE)  No valid dispenser profile\n"
        "\n"
        "Cleared since your last email\n"
        "  COM-101  Vending board heartbeat lost\n"
        "\n"
        "Open the machine: https://cloud.example/m/abcd-efgh-ijkl-mnop\n"
        "You receive this as owner of this machine. "
        "Mute: https://cloud.example/machines/abcd-efgh-ijkl-mnop/people\n"
    )


def test_golden_offline():
    msg = build([Ev("offline")], [], role="tech")
    assert msg["Subject"] == "[ice-colder] Lobby (abcd-efgh-ijkl-mnop): offline"
    assert msg.get_content() == (
        "Machine Lobby (abcd-efgh-ijkl-mnop)\n"
        "Stopped reporting at 2026-10-19 14:02 UTC (last report 14:00)\n"
        "\n"
        "Open the machine: https://cloud.example/m/abcd-efgh-ijkl-mnop\n"
        "You receive this as technician of this machine. "
        "Mute: https://cloud.example/machines/abcd-efgh-ijkl-mnop/people\n"
    )


def test_golden_online():
    msg = build([Ev("online")], [])
    assert msg["Subject"] == "[ice-colder] Lobby (abcd-efgh-ijkl-mnop): back online"
    assert (
        msg.get_content().splitlines()[1] == "Reporting again at 2026-10-19 14:02 UTC"
    )


def test_subject_forms():
    assert build([Ev("offline")], [])["Subject"].endswith(": offline")
    assert build([Ev("online")], [])["Subject"].endswith(": back online")
    assert build(RAISED[:1], [])["Subject"].endswith(": 1 raised, 0 cleared")


def test_links_omitted_without_public_url():
    body = build(RAISED, CLEARED, public_url=None).get_content()
    assert "Open the machine" not in body
    assert "Mute:" not in body
    assert body.rstrip().endswith("You receive this as owner of this machine.")


def test_offline_without_last_report():
    body = build([Ev("offline")], [], last_report_at=None).get_content()
    assert "Stopped reporting at 2026-10-19 14:02 UTC\n" in body


def test_make_mailer_log_only_when_unconfigured():
    assert isinstance(make_mailer(SimpleNamespace(smtp_host=None)), LogOnlyMailer)
    assert isinstance(make_mailer(SimpleNamespace()), LogOnlyMailer)


def test_make_mailer_smtp_when_configured():
    s = SimpleNamespace(
        smtp_host="h",
        smtp_port=587,
        smtp_user="u",
        smtp_password="p",
        smtp_from="f@x",
        smtp_starttls=True,
    )
    assert isinstance(make_mailer(s), SmtpMailer)


async def test_smtp_mailer_calls_smtp_send_with_settings(monkeypatch):
    calls = []
    stub = types.ModuleType("ice_common.mailer")
    stub.smtp_send = lambda **kw: calls.append(kw)
    monkeypatch.setitem(sys.modules, "ice_common.mailer", stub)
    cfg = SmtpConfig("h", 2525, "u", "p", False, "f@x")
    msg = build(RAISED, [])
    await SmtpMailer(cfg).send(msg)
    assert calls == [
        dict(
            host="h", port=2525, username="u", password="p", use_starttls=False, msg=msg
        )
    ]


async def test_recording_mailer_fail_next():
    m = RecordingMailer()
    msg = build(RAISED, [])
    await m.send(msg)
    m.fail_next = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await m.send(msg)
    assert m.fail_next is None
    await m.send(msg)
    assert m.sent == [msg, msg]


async def test_log_only_mailer_does_not_raise():
    await LogOnlyMailer().send(build(RAISED, []))
