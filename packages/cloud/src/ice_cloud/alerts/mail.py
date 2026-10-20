"""Mailer protocol, implementations and alert email rendering (alerts spec 3.4, 3.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Protocol

from loguru import logger

_ROLE_WORDS = {"owner": "owner", "tech": "technician"}


class MailEvent(Protocol):
    """The slice of an alert event that the email needs."""

    @property
    def kind(self) -> str: ...
    @property
    def at(self) -> datetime: ...
    @property
    def code(self) -> str | None: ...
    @property
    def severity(self) -> str | None: ...
    @property
    def scope(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def sku(self) -> str | None: ...
    @property
    def product(self) -> str | None: ...


class Mailer(Protocol):
    async def send(self, msg: EmailMessage) -> None:
        """Deliver one message; raises on failure."""
        ...


@dataclass(frozen=True)
class SmtpConfig:
    host: str | None
    port: int
    username: str | None
    password: str | None
    use_starttls: bool
    from_addr: str | None

    @classmethod
    def from_settings(cls, settings: Any) -> SmtpConfig:
        port = getattr(settings, "smtp_port", None)
        starttls = getattr(settings, "smtp_starttls", None)
        return cls(
            host=getattr(settings, "smtp_host", None),
            port=587 if port is None else port,
            username=getattr(settings, "smtp_user", None),
            password=getattr(settings, "smtp_password", None),
            use_starttls=True if starttls is None else starttls,
            from_addr=getattr(settings, "smtp_from", None),
        )


class SmtpMailer:
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    async def send(self, msg: EmailMessage) -> None:
        from ice_common.mailer import smtp_send

        cfg = self._config
        await asyncio.to_thread(
            smtp_send,
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password,
            use_starttls=cfg.use_starttls,
            msg=msg,
        )


class LogOnlyMailer:
    async def send(self, msg: EmailMessage) -> None:
        logger.info("Alert email (SMTP unset): {} -> {}", msg["Subject"], msg["To"])


class RecordingMailer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next: Exception | None = None

    async def send(self, msg: EmailMessage) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.sent.append(msg)


def make_mailer(settings: Any) -> Mailer:
    config = SmtpConfig.from_settings(settings)
    if config.host is None:
        return LogOnlyMailer()
    return SmtpMailer(config)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _scope_text(event: MailEvent) -> str:
    scope = event.scope or ("product" if event.sku else "machine")
    if scope == "product" and event.sku:
        return f"product {event.product or event.sku} (sku {event.sku})"
    return scope


def render_email(
    *,
    label: str,
    short: str,
    role: str,
    triggering: Sequence[MailEvent],
    cleared: Sequence[MailEvent],
    public_url: str | None,
    from_addr: str,
    to: str,
    last_report_at: datetime | None,
) -> EmailMessage:
    kinds = {e.kind for e in triggering}
    head = f"[ice-colder] {label} ({short}): "
    lines = [f"Machine {label} ({short})"]
    raised = [e for e in triggering if e.kind == "raised"]
    if "offline" in kinds or "online" in kinds:
        back = "online" in kinds
        subject = head + ("back online" if back else "offline")
        for event in (e for e in triggering if e.kind == "offline"):
            line = f"Stopped reporting at {_utc(event.at)}"
            if last_report_at is not None:
                line += f" (last report {_as_utc(last_report_at):%H:%M})"
            lines.append(line)
            break
        for event in (e for e in triggering if e.kind == "online"):
            lines.append(f"Reporting again at {_utc(event.at)}")
            break
    else:
        subject = head + f"{len(raised)} raised, {len(cleared)} cleared"
        at = (raised or list(cleared) or list(triggering))[0].at
        lines.append(f"Fault changes at {_utc(at)}")

    if raised:
        lines += ["", "Raised"]
        for e in raised:
            lines.append(f"  {e.code}  {e.severity}  {_scope_text(e)}  {e.description}")
    if cleared:
        lines += ["", "Cleared since your last email"]
        for e in cleared:
            lines.append(f"  {e.code}  {e.description}")

    lines.append("")
    base = public_url.rstrip("/") if public_url else None
    if base:
        lines.append(f"Open the machine: {base}/m/{short}")
    footer = f"You receive this as {_ROLE_WORDS.get(role, role)} of this machine."
    if base:
        footer += f" Mute: {base}/machines/{short}/people"
    lines.append(footer)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.set_content("\n".join(lines) + "\n")
    return msg
