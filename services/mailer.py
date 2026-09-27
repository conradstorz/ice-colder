"""One place that sends email, so nothing in the app blocks on SMTP.

Extracted from services/notifier.py, which now calls it. OTP delivery and the
machine report use it too. A send never raises: the caller gets False and the
UI offers the offline path instead (spec §6).
"""

import asyncio
import smtplib
from email.message import EmailMessage

from loguru import logger

SMTP_TIMEOUT_SECONDS = 15.0


def smtp_send(email_config, msg: EmailMessage) -> None:
    """Blocking SMTP send. Call through send_email, or from an executor."""
    with smtplib.SMTP(
        email_config.smtp_server, email_config.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
    ) as server:
        server.starttls()
        server.login(email_config.username, email_config.password.get_secret_value())
        server.send_message(msg)


async def send_email(
    email_config,
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    """Send one email in a thread. True on success, False on any failure.

    Args:
        email_config: Email gateway configuration.
        to: Recipient email address.
        subject: Email subject.
        body: Email body (plain text).
        attachments: Optional list of (filename, payload, mime_type) tuples.
                    When None (default), message remains single-part.
                    Each mime_type must contain '/' (e.g., 'text/csv').
                    If a mime_type is malformed, returns False.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_config.default_from
    msg["To"] = to
    msg.set_content(body)

    # Add attachments if provided
    if attachments:
        for filename, payload, mime_type in attachments:
            # Validate and parse mime type
            if "/" not in mime_type:
                logger.error(
                    f"mailer: malformed mime type '{mime_type}' for attachment '{filename}'"
                )
                return False

            maintype, subtype = mime_type.split("/", 1)
            msg.add_attachment(
                payload, maintype=maintype, subtype=subtype, filename=filename
            )

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, smtp_send, email_config, msg)
    except Exception as e:
        logger.error(f"mailer: send to {to} failed: {e}")
        return False
    logger.info(f"mailer: email sent to {to}")
    return True
