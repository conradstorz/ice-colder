"""Ed25519 request signing for the edge-to-cloud channel (spec section 4.2).

Pure helpers: no I/O, no logging. The edge signs each request with its private
key; the cloud verifies against the machine's enrolled public key.
"""

import base64
import binascii
import re
from datetime import UTC, datetime
from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

HEADER_MACHINE = "X-Ice-Machine"
HEADER_TIMESTAMP = "X-Ice-Timestamp"
HEADER_SIGNATURE = "X-Ice-Signature"
MAX_CLOCK_SKEW_SECONDS = 300

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SHORT_FP_HEX = 16
_HEX16 = re.compile(r"[0-9a-f]{16}")


def generate_private_key() -> bytes:
    """Return a new Ed25519 private key as 32 raw bytes."""
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def public_key_from_private(private: bytes) -> bytes:
    """Return the 32 raw public-key bytes for a 32-byte private key."""
    return (
        Ed25519PrivateKey.from_private_bytes(private)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def fingerprint(public_key: bytes) -> str:
    """Return the SHA-256 of the public key as 64 lower-case hex characters."""
    return sha256(public_key).hexdigest()


def short_fingerprint(fp: str) -> str:
    """Format the first 16 hex characters of a fingerprint as xxxx-xxxx-xxxx-xxxx."""
    head = fp[:_SHORT_FP_HEX]
    return "-".join(head[i : i + 4] for i in range(0, _SHORT_FP_HEX, 4))


def parse_short_fingerprint(text: str) -> str | None:
    """Return 16 lower-case hex characters from typed input, or None if invalid.

    Dashes and spaces are ignored and upper case is accepted.
    """
    cleaned = re.sub(r"[-\s]", "", text).lower()
    return cleaned if _HEX16.fullmatch(cleaned) else None


def canonical_message(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """Build the exact bytes that are signed for one request."""
    return f"{method.upper()}\n{path}\n{timestamp}\n{sha256(body).hexdigest()}".encode()


def sign(private_key: bytes, message: bytes) -> str:
    """Sign a message and return the signature as standard base64."""
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
    return base64.b64encode(signature).decode("ascii")


def verify(public_key: bytes, message: bytes, signature_b64: str) -> bool:
    """Return True only for a valid signature; never raises."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
    return True


def format_timestamp(now: datetime) -> str:
    """Format an aware datetime as UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
    return now.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def is_timestamp_fresh(timestamp: str, now: datetime) -> bool:
    """Return True if the timestamp parses and is within the allowed clock skew."""
    try:
        parsed = datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return abs((now - parsed).total_seconds()) <= MAX_CLOCK_SKEW_SECONDS
