"""Cloud configuration read once from the environment (identity spec section 8)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ice_contracts.cloud.ui import DIGEST_PATTERN

MIN_SESSION_KEY_BYTES = 32


class SettingsError(ValueError):
    """The environment cannot start the cloud; the message says why."""


@dataclass(frozen=True)
class CloudSettings:
    data_dir: Path
    public_url: str | None
    google_client_id: str | None
    google_client_secret: str | None
    admins: frozenset[str]
    session_key: bytes
    insecure_cookies: bool = False
    trusted_ui_digests: frozenset[str] = frozenset()
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    max_unclaimed_machines: int = 200
    unclaimed_ttl_days: int = 7
    max_unclaimed_report_bytes: int = 16 * 1024

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def smtp_configured(self) -> bool:
        return self.smtp_host is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CloudSettings:
        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        raw_key = env.get("ICE_CLOUD_SESSION_KEY", "")
        if not raw_key:
            raise SettingsError("ICE_CLOUD_SESSION_KEY is required")
        key = raw_key.encode("utf-8")
        if len(key) < MIN_SESSION_KEY_BYTES:
            raise SettingsError(
                f"ICE_CLOUD_SESSION_KEY must be at least {MIN_SESSION_KEY_BYTES} bytes"
            )

        client_id = get("ICE_CLOUD_GOOGLE_CLIENT_ID")
        client_secret = get("ICE_CLOUD_GOOGLE_CLIENT_SECRET")
        if bool(client_id) != bool(client_secret):
            raise SettingsError(
                "ICE_CLOUD_GOOGLE_CLIENT_ID and ICE_CLOUD_GOOGLE_CLIENT_SECRET "
                "must be set together"
            )
        public_url = get("ICE_CLOUD_PUBLIC_URL")
        if client_id and not public_url:
            raise SettingsError(
                "ICE_CLOUD_PUBLIC_URL is required when Google sign-in is configured"
            )

        entries = (
            p.strip().lower() for p in env.get("ICE_CLOUD_ADMINS", "").split(",")
        )
        digests = [
            p.strip().lower()
            for p in env.get("ICE_CLOUD_TRUSTED_UI_DIGESTS", "").split(",")
        ]
        for digest in digests:
            if digest and not re.fullmatch(DIGEST_PATTERN, digest):
                raise SettingsError(
                    f"ICE_CLOUD_TRUSTED_UI_DIGESTS entry {digest!r} is not a"
                    " sha256 hex digest"
                )

        smtp_host = get("ICE_CLOUD_SMTP_HOST")
        smtp_from = get("ICE_CLOUD_SMTP_FROM")
        if smtp_host and not smtp_from:
            raise SettingsError(
                "ICE_CLOUD_SMTP_FROM is required when ICE_CLOUD_SMTP_HOST is set"
            )
        smtp_user = get("ICE_CLOUD_SMTP_USER")
        smtp_password = get("ICE_CLOUD_SMTP_PASSWORD")
        if smtp_user and not smtp_password:
            raise SettingsError(
                "ICE_CLOUD_SMTP_PASSWORD is required when ICE_CLOUD_SMTP_USER is set"
            )
        raw_port = get("ICE_CLOUD_SMTP_PORT")
        smtp_port = 587
        if raw_port is not None:
            if not (re.fullmatch(r"[0-9]+", raw_port) and 1 <= int(raw_port) <= 65535):
                raise SettingsError(
                    f"ICE_CLOUD_SMTP_PORT {raw_port!r} is not a port number (1-65535)"
                )
            smtp_port = int(raw_port)

        def positive(name: str, default: int) -> int:
            raw = get(name)
            if raw is None:
                return default
            if not (re.fullmatch(r"[0-9]+", raw) and int(raw) >= 1):
                raise SettingsError(f"{name} {raw!r} is not a positive integer")
            return int(raw)

        return cls(
            data_dir=Path(env.get("ICE_CLOUD_DATA") or "cloud_data"),
            public_url=public_url,
            google_client_id=client_id,
            google_client_secret=client_secret,
            admins=frozenset(e for e in entries if e),
            session_key=key,
            insecure_cookies=env.get("ICE_CLOUD_INSECURE_COOKIES", "").strip() == "1",
            trusted_ui_digests=frozenset(d for d in digests if d),
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=get("ICE_CLOUD_SMTP_USER"),
            smtp_password=get("ICE_CLOUD_SMTP_PASSWORD"),
            smtp_from=smtp_from,
            smtp_starttls=env.get("ICE_CLOUD_SMTP_STARTTLS", "").strip() != "0",
            max_unclaimed_machines=positive("ICE_CLOUD_MAX_UNCLAIMED_MACHINES", 200),
            unclaimed_ttl_days=positive("ICE_CLOUD_UNCLAIMED_TTL_DAYS", 7),
            max_unclaimed_report_bytes=positive(
                "ICE_CLOUD_MAX_UNCLAIMED_REPORT_BYTES", 16 * 1024
            ),
        )
