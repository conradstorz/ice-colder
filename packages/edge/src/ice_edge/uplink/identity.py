"""The machine's cloud identity: an Ed25519 keypair kept in a private file.

The file also holds the one pending claim-code notice the uplink retries until
the cloud acknowledges it (spec sections 4.1 and 5.3). The key is generated
once; a corrupt file raises ``IdentityError`` and is never regenerated.
"""

import base64
import binascii
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ice_common.paths import DATA_DIR, fsync_dir
from ice_contracts.cloud import signing

IDENTITY_FILE = DATA_DIR / "cloud_identity.json"

_VERSION = 1


class IdentityError(RuntimeError):
    """The identity file exists but cannot be trusted."""


@dataclass(frozen=True)
class ClaimNotice:
    """The claim-code state the cloud must be told about.

    ``claim_code_hash`` of None means "no live code" (a pending DELETE).
    """

    claim_code_hash: str | None
    expires_at: str | None
    acked: bool


class MachineIdentity:
    def __init__(
        self,
        path: Path,
        private_key: bytes,
        created_at: str,
        notice: ClaimNotice | None,
    ) -> None:
        self._path = path
        self._private_key = private_key
        self._public_key = signing.public_key_from_private(private_key)
        self._created_at = created_at
        self._notice = notice
        self._fingerprint = signing.fingerprint(self._public_key)

    @classmethod
    def load_or_create(cls, path: Path = IDENTITY_FILE) -> "MachineIdentity":
        if path.exists():
            return cls._load(path)
        identity = cls(
            path,
            signing.generate_private_key(),
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            None,
        )
        identity._write(None)
        return identity

    @classmethod
    def _load(cls, path: Path) -> "MachineIdentity":
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            private = base64.b64decode(doc["private_key_b64"], validate=True)
            public = base64.b64decode(doc["public_key_b64"], validate=True)
            if doc["version"] != _VERSION:
                raise ValueError(f"unsupported version {doc['version']!r}")
            if signing.public_key_from_private(private) != public:
                raise ValueError("public key does not match private key")
            created_at = doc["created_at"]
            if not isinstance(created_at, str):
                raise ValueError("created_at must be a string")
            raw = doc["notice"]
            notice = None
            if raw is not None:
                notice = ClaimNotice(
                    raw["claim_code_hash"], raw["expires_at"], bool(raw["acked"])
                )
        except (OSError, ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise IdentityError(f"identity file {path} is unusable: {exc}") from exc
        return cls(path, private, created_at, notice)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def short_fingerprint(self) -> str:
        return signing.short_fingerprint(self._fingerprint)

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self._public_key).decode("ascii")

    @property
    def notice(self) -> ClaimNotice | None:
        return self._notice

    def sign(self, method: str, path: str, timestamp: str, body: bytes) -> str:
        message = signing.canonical_message(method, path, timestamp, body)
        return signing.sign(self._private_key, message)

    def set_notice(self, claim_code_hash: str | None, expires_at: str | None) -> None:
        """Record the newest notice (unacked), replacing any previous one."""
        notice = ClaimNotice(claim_code_hash, expires_at, False)
        self._write(notice)
        self._notice = notice

    def mark_notice_acked(self, notice: ClaimNotice) -> bool:
        """Ack ``notice`` only if it is still the current one.

        A notice replaced while its request was in flight stays unacked so the
        newer one is sent. Returns True when the ack was recorded.
        """
        if self._notice != notice:
            return False
        acked = replace(notice, acked=True)
        self._write(acked)
        self._notice = acked
        return True

    def _write(self, notice: ClaimNotice | None) -> None:
        doc = {
            "version": _VERSION,
            "private_key_b64": base64.b64encode(self._private_key).decode("ascii"),
            "public_key_b64": self.public_key_b64,
            "created_at": self._created_at,
            "notice": None
            if notice is None
            else {
                "claim_code_hash": notice.claim_code_hash,
                "expires_at": notice.expires_at,
                "acked": notice.acked,
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        if os.name == "posix":
            os.chmod(self._path, 0o600)
            fsync_dir(self._path.parent)
