# ice_edge/notify/notifier.py
"""
Owner notification service.

Routes alerts to the machine owner via their preferred communication channel.
Currently supports logging (always) and email (when configured).
Deduplicates alerts to avoid flooding the owner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING

from loguru import logger

from ice_common.config_model import _is_placeholder_host, ConfigModel, Channel
from ice_edge.notify.mailer import smtp_send

if TYPE_CHECKING:
    from ice_edge.monitoring.health_monitor import Alert


class Notifier:
    """
    Delivers health alerts to the machine owner.

    Usage:
        notifier = Notifier(config)
        await notifier.send(alert)
    """

    RETRY_SECONDS: float = 60.0  # min gap between attempts after a failed send

    def __init__(
        self,
        config: ConfigModel,
        *,
        cloud_handles_alerts: Callable[[], bool] = lambda: False,
    ):
        self._cloud_handles_alerts = cloud_handles_alerts
        self._config = config
        self._owner = config.machine_owner
        # Rate limiting: last send time per (source, code|message, sku)
        self._last_sent: dict[str, float] = {}
        self._cooldown_seconds: float = (
            300.0  # 5 min between repeat alerts per (source, code|message, sku)
        )
        self._last_failed: dict[str, float] = {}
        self._warned_unconfigured = False

    def set_cloud_handles_alerts(self, predicate: Callable[[], bool]) -> None:
        """Attach the cloud hand-off predicate once the uplink identity exists."""
        self._cloud_handles_alerts = predicate

    @staticmethod
    def _cooldown_key(alert: Alert) -> str:
        """One cooldown bucket per distinct fault, not per source.

        Every registry fault is raised with source="vmc"; keying on source
        alone silently dropped any second fault inside the cooldown.
        """
        return f"{alert.source}|{alert.code or alert.message}|{alert.product_sku or ''}"

    async def send(self, alert: Alert) -> bool:
        """
        Route an alert to the owner via their preferred channel.
        Runs blocking I/O (SMTP) in a thread executor to stay async.

        Returns True when the alert is handled (delivered, log-only, deferred to
        the cloud, or suppressed by the success cooldown); False when a
        configured email send failed or a retry is still pending.
        """
        now = asyncio.get_running_loop().time()
        key = self._cooldown_key(alert)
        last = self._last_sent.get(key)
        # `last is None` must mean "never sent": loop.time() is monotonic and can
        # be small on a freshly booted host, so a 0.0 sentinel would wrongly
        # suppress the first alert from every key for a whole cooldown.
        if last is not None and now - last < self._cooldown_seconds:
            logger.debug(f"Notifier: Suppressing alert {key} (cooldown)")
            return True
        failed = self._last_failed.get(key)
        if failed is not None and now - failed < self.RETRY_SECONDS:
            logger.debug(f"Notifier: Waiting to retry alert {key}")
            return False

        try:
            deferred = self._cloud_handles_alerts()
        except Exception:
            logger.exception(
                "Notifier: cloud hand-off check failed; delivering locally"
            )
            deferred = False
        if deferred:
            logger.info(f"Notifier: deferred to cloud: {key}")
            self._last_sent[key] = now
            return True

        logger.info(
            f"Notifier: [{alert.level}] {alert.source} -> {self._owner.name}: "
            f"{alert.message}"
        )
        if await self._deliver(alert):
            self._last_sent[key] = now
            self._last_failed.pop(key, None)
            return True
        self._last_failed[key] = now
        return False

    async def _deliver(self, alert: Alert) -> bool:
        """Pick the owner's channel and send. Split out so tests can stub delivery."""
        gateway_info = self._config.get_preferred_gateway_for(self._owner)
        if gateway_info is None:
            logger.warning("Notifier: No configured gateway for owner")
            return True

        channel, gateway_config = gateway_info

        if channel == Channel.email:
            if not gateway_config.is_configured or _is_placeholder_host(
                self._owner.email
            ):
                if not self._warned_unconfigured:
                    logger.warning(
                        "Notifier: email gateway not configured (placeholder "
                        "smtp server or owner address); alerts are logged only"
                    )
                    self._warned_unconfigured = True
                return True
            return await self._send_email(alert, gateway_config)
        elif channel == Channel.sms:
            logger.info(f"Notifier: SMS alert would be sent to {self._owner.phone}")
            # SMS integration is a future task
        else:
            logger.info(f"Notifier: Channel {channel} not yet implemented")
        return True

    async def _send_email(self, alert: Alert, email_config) -> bool:
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

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, smtp_send, email_config, msg)
            logger.info(f"Notifier: Email sent to {self._owner.email}")
            return True
        except Exception as e:
            logger.error(f"Notifier: Email send failed: {e}")
            return False
