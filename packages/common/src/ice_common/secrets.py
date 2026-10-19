"""Salted scrypt hashes for short codes, shared by the edge and the cloud."""

from __future__ import annotations

import hashlib
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _scrypt(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_secret(value: str) -> str:
    """Salted scrypt for codes stored without a separate salt column."""
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(value, salt).hex()}"


def verify_secret(value: str, stored: str) -> bool:
    """Constant-time check against hash_secret output. Garbage returns False."""
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(_scrypt(value, salt).hex(), digest_hex)
