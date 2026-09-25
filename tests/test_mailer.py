"""Tests for services/mailer.py."""

from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from config.config_model import EmailGatewayConfig
from services.mailer import SMTP_TIMEOUT_SECONDS, send_email, smtp_send


@pytest.fixture
def gateway():
    return EmailGatewayConfig(
        smtp_server="smtp.test.local",
        smtp_port=587,
        username="vmc@test.local",
        default_from="vmc@test.local",
    )


def test_smtp_send_uses_starttls_login_and_the_timeout(gateway):
    msg = EmailMessage()
    with patch("services.mailer.smtplib.SMTP") as smtp:
        server = smtp.return_value.__enter__.return_value
        smtp_send(gateway, msg)
    smtp.assert_called_once_with("smtp.test.local", 587, timeout=SMTP_TIMEOUT_SECONDS)
    server.starttls.assert_called_once()
    server.login.assert_called_once()
    server.send_message.assert_called_once_with(msg)


async def test_send_email_builds_the_message_and_reports_success(gateway):
    sent = {}

    def fake_send(cfg, msg):
        sent["to"] = msg["To"]
        sent["from"] = msg["From"]
        sent["subject"] = msg["Subject"]
        sent["body"] = msg.get_content()

    with patch("services.mailer.smtp_send", side_effect=fake_send):
        ok = await send_email(gateway, "ada@example.com", "Your code", "123456")

    assert ok is True
    assert sent["to"] == "ada@example.com"
    assert sent["from"] == "vmc@test.local"
    assert sent["subject"] == "Your code"
    assert "123456" in sent["body"]


async def test_send_email_swallows_failures_and_returns_false(gateway, caplog):
    with patch("services.mailer.smtp_send", side_effect=OSError("no route to host")):
        ok = await send_email(gateway, "ada@example.com", "Your code", "123456")
    assert ok is False
    assert "no route to host" in caplog.text


async def test_send_email_never_blocks_the_loop(gateway):
    """The blocking send must go through an executor, not run inline."""
    calls = []

    def fake_send(cfg, msg):
        calls.append(MagicMock())

    with patch("services.mailer.smtp_send", side_effect=fake_send):
        assert await send_email(gateway, "a@b.c", "s", "b") is True
    assert len(calls) == 1
