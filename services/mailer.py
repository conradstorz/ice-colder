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


async def send_email(email_config, to: str, subject: str, body: str) -> bool:
    """Send one plain-text email in a thread. True on success, False on any failure."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_config.default_from
    msg["To"] = to
    msg.set_content(body)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, smtp_send, email_config, msg)
    except Exception as e:
        logger.error(f"mailer: send to {to} failed: {e}")
        return False
    logger.info(f"mailer: email sent to {to}")
    return True
