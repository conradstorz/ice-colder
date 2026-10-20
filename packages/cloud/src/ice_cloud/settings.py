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

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

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
        return cls(
            data_dir=Path(env.get("ICE_CLOUD_DATA") or "cloud_data"),
            public_url=public_url,
            google_client_id=client_id,
            google_client_secret=client_secret,
            admins=frozenset(e for e in entries if e),
            session_key=key,
            insecure_cookies=env.get("ICE_CLOUD_INSECURE_COOKIES", "").strip() == "1",
            trusted_ui_digests=frozenset(d for d in digests if d),
        )
