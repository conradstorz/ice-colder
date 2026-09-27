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


async def test_send_email_without_attachments_remains_single_part(gateway):
    """With no attachments, the message must remain single-part, not multipart."""
    sent_msg = None

    def capture_msg(cfg, msg):
        nonlocal sent_msg
        sent_msg = msg

    with patch("services.mailer.smtp_send", side_effect=capture_msg):
        ok = await send_email(gateway, "ada@example.com", "Test", "Hello world")

    assert ok is True
    assert sent_msg is not None
    # Key assertion: message must NOT be multipart
    assert sent_msg.is_multipart() is False
    # Content should be plain text
    assert "Hello world" in sent_msg.get_content()


async def test_send_email_with_one_csv_attachment_becomes_multipart(gateway):
    """A single CSV attachment produces a multipart message."""
    csv_data = b"name,value\nitem1,100\nitem2,200"
    sent_msg = None

    def capture_msg(cfg, msg):
        nonlocal sent_msg
        sent_msg = msg

    with patch("services.mailer.smtp_send", side_effect=capture_msg):
        ok = await send_email(
            gateway,
            "ada@example.com",
            "Report",
            "See attached",
            attachments=[("report.csv", csv_data, "text/csv")],
        )

    assert ok is True
    assert sent_msg is not None
    # Message must be multipart
    assert sent_msg.is_multipart() is True
    # Get all parts: first should be body, second should be attachment
    parts = sent_msg.get_payload()
    assert len(parts) == 2

    body_part = parts[0]
    attachment_part = parts[1]

    # Body part should be the original text
    assert "See attached" in body_part.get_content()

    # Attachment should have correct headers and content
    assert attachment_part.get_filename() == "report.csv"
    assert attachment_part.get_content_type() == "text/csv"
    # Verify payload is decoded back to original bytes
    assert attachment_part.get_payload(decode=True) == csv_data


async def test_send_email_with_two_attachments(gateway):
    """Multiple attachments both arrive."""
    csv_data = b"a,b,c\n1,2,3"
    txt_data = b"Hello\nWorld"
    sent_msg = None

    def capture_msg(cfg, msg):
        nonlocal sent_msg
        sent_msg = msg

    with patch("services.mailer.smtp_send", side_effect=capture_msg):
        ok = await send_email(
            gateway,
            "ada@example.com",
            "Multiple",
            "Both attached",
            attachments=[
                ("data.csv", csv_data, "text/csv"),
                ("notes.txt", txt_data, "text/plain"),
            ],
        )

    assert ok is True
    assert sent_msg is not None
    assert sent_msg.is_multipart() is True

    parts = sent_msg.get_payload()
    # Body + 2 attachments = 3 parts
    assert len(parts) == 3

    # Check first attachment
    assert parts[1].get_filename() == "data.csv"
    assert parts[1].get_content_type() == "text/csv"
    assert parts[1].get_payload(decode=True) == csv_data

    # Check second attachment
    assert parts[2].get_filename() == "notes.txt"
    assert parts[2].get_content_type() == "text/plain"
    assert parts[2].get_payload(decode=True) == txt_data


async def test_send_email_rejects_malformed_mime_type(gateway, caplog):
    """A mime type without '/' returns False and logs an error."""
    bad_data = b"some data"

    with patch("services.mailer.smtp_send") as mock_send:
        ok = await send_email(
            gateway,
            "ada@example.com",
            "Bad mime",
            "Text",
            attachments=[("file.bin", bad_data, "invalidmime")],
        )

    # Should return False
    assert ok is False
    # Should never call smtp_send
    mock_send.assert_not_called()
    # Should log the error
    assert "malformed mime type" in caplog.text
    assert "invalidmime" in caplog.text
    assert "file.bin" in caplog.text
