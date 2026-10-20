"""Blocking SMTP send shared by the edge and the cloud.

Primitives only, so ice_common does not depend on any app's config model. Raises on
failure; each caller decides whether a failed send is fatal (the edge never lets it
block a sale).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

SMTP_TIMEOUT_SECONDS = 15.0


def smtp_send(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_starttls: bool,
    msg: EmailMessage,
    smtp_factory=smtplib.SMTP,
    timeout: float = SMTP_TIMEOUT_SECONDS,
) -> None:
    """Connect, optionally upgrade to TLS, optionally log in, then send ``msg``."""
    with smtp_factory(host, port, timeout=timeout) as server:
        if use_starttls:
            server.starttls()
        if username is not None:
            server.login(username, password)
        server.send_message(msg)
