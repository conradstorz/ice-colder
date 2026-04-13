# services/notifier.py
"""
Owner notification service.

Routes alerts to the machine owner via their preferred communication channel.
Currently supports logging (always) and email (when configured).
Deduplicates alerts to avoid flooding the owner.
"""
import asyncio
import smtplib
from email.message import EmailMessage
from typing import Optional

from loguru import logger

from config.config_model import ConfigModel, Channel
from services.health_monitor import Alert


class Notifier:
    """
    Delivers health alerts to the machine owner.

    Usage:
        notifier = Notifier(config)
        await notifier.send(alert)
    """

    def __init__(self, config: ConfigModel):
        self._config = config
        self._owner = config.machine_owner
        # Rate limiting: track last send time per alert source
        self._last_sent: dict[str, float] = {}
        self._cooldown_seconds: float = 300.0  # 5 min between repeat alerts per source

    async def send(self, alert: Alert):
        """
        Route an alert to the owner via their preferred channel.
        Runs blocking I/O (SMTP) in a thread executor to stay async.
        """
        # Rate-limit per source
        now = asyncio.get_event_loop().time()
        last = self._last_sent.get(alert.source, 0.0)
        if now - last < self._cooldown_seconds:
            logger.debug(f"Notifier: Suppressing alert from {alert.source} (cooldown)")
            return
        self._last_sent[alert.source] = now

        logger.info(
            f"Notifier: [{alert.level}] {alert.source} -> {self._owner.name}: "
            f"{alert.message}"
        )

        gateway_info = self._config.get_preferred_gateway_for(self._owner)
        if gateway_info is None:
            logger.warning("Notifier: No configured gateway for owner")
            return

        channel, gateway_config = gateway_info

        if channel == Channel.email:
            await self._send_email(alert, gateway_config)
        elif channel == Channel.sms:
            logger.info(f"Notifier: SMS alert would be sent to {self._owner.phone}")
            # SMS integration is a future task
        else:
            logger.info(f"Notifier: Channel {channel} not yet implemented")

    async def _send_email(self, alert: Alert, email_config):
        """Send an alert email. Runs SMTP in executor to avoid blocking."""
        subject = f"[VMC {alert.level.upper()}] {alert.source}: {alert.message[:60]}"
        body = (
            f"VMC Health Alert\n"
            f"================\n\n"
            f"Level:   {alert.level}\n"
            f"Source:  {alert.source}\n"
            f"Message: {alert.message}\n"
        )

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = email_config.default_from
        msg["To"] = self._owner.email
        msg.set_content(body)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._smtp_send, email_config, msg)
            logger.info(f"Notifier: Email sent to {self._owner.email}")
        except Exception as e:
            logger.error(f"Notifier: Email send failed: {e}")

    @staticmethod
    def _smtp_send(email_config, msg: EmailMessage):
        """Blocking SMTP send (called in executor)."""
        with smtplib.SMTP(email_config.smtp_server, email_config.smtp_port) as server:
            server.starttls()
            server.login(
                email_config.username,
                email_config.password.get_secret_value(),
            )
            server.send_message(msg)
