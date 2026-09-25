"""Named users, roles, PIN login, device trust and back-off for the dashboard.

Everything access-related lives here: the ``Backoff`` rate limiter, PIN and
code hashing, the permission table, and the ``AccessStore`` persisted to
``data/access.json``. That file holds secrets and PII and is never merged
into config.json.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import time
from collections import deque
from enum import Enum
from typing import Callable, Optional

from loguru import logger

BACKOFF_CAP_SECONDS = 3600.0
BUDGET_THRESHOLD = 20
BUDGET_WINDOW_SECONDS = 3600.0
BACKOFF_PRUNE_SECONDS = 86400.0

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class Role(str, Enum):
    owner = "owner"
    secretary = "secretary"
    tech = "tech"
    loader = "loader"


class Permission(str, Enum):
    view_status = "view_status"
    clear_faults = "clear_faults"
    view_logs = "view_logs"
    machine_controls = "machine_controls"
    run_tests = "run_tests"
    edit_catalog = "edit_catalog"
    edit_placement = "edit_placement"
    view_reports = "view_reports"
    edit_contacts = "edit_contacts"
    edit_secrets = "edit_secrets"
    manage_users = "manage_users"
    manage_ownership = "manage_ownership"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.owner: frozenset(Permission),
    Role.secretary: frozenset(
        {
            Permission.view_status,
            Permission.edit_catalog,
            Permission.edit_placement,
            Permission.view_reports,
            Permission.edit_contacts,
            Permission.manage_users,
        }
    ),
    Role.tech: frozenset(
        {
            Permission.view_status,
            Permission.clear_faults,
            Permission.view_logs,
            Permission.machine_controls,
            Permission.run_tests,
            Permission.edit_placement,
        }
    ),
    Role.loader: frozenset({Permission.view_status, Permission.edit_placement}),
}


def _scrypt(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    """Return (pin_hash_hex, salt_hex); a fresh random salt unless one is given."""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    return _scrypt(pin, salt_bytes).hex(), salt_bytes.hex()


def verify_pin(pin: str, pin_hash: str, pin_salt: str) -> bool:
    try:
        candidate, _ = hash_pin(pin, pin_salt)
    except ValueError:
        return False
    return secrets.compare_digest(candidate, pin_hash)


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


def generate_code(digits: int) -> str:
    """A zero-padded random decimal code of exactly *digits* digits."""
    return str(secrets.randbelow(10**digits)).zfill(digits)


def generate_token() -> str:
    """A random 256-bit URL-safe token for cookies and session ids."""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """sha256 of a cookie value — what the store keeps instead of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Backoff:
    """Exponential back-off per (kind, subject, client), plus a per-user budget.

    After ``n`` consecutive failures the next attempt waits
    ``min(2 ** (n - 1), 3600)`` seconds. A success resets that key. Failures
    from clients where the subject user is not trusted also feed a per-user
    budget, so a distributed attacker cannot buy fresh counters with fresh
    IPs; attempts from a client the user is trusted on never touch it, so a
    stranger can never slow the legitimate user on their own tablet.

    There are no hard caps, no per-user disabling and no IP lockout.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._clock = clock
        # (kind, subject, client) -> (consecutive_failures, last_failure_at)
        self._failures: dict[tuple[str, str, str], tuple[int, float]] = {}
        # (kind, subject) -> timestamps of untrusted failures inside the window
        self._budget: dict[tuple[str, str], deque[float]] = {}
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(f"Backoff: ignoring invalid trusted proxy CIDR {cidr!r}")

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to key on when no stored device resolves.

        Only when the socket peer is a configured proxy is X-Forwarded-For
        consulted, and then its rightmost entry: the hop that proxy appended.
        Anything a client supplied itself sits to the left and is ignored.
        """
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        return hops[-1] if hops else peer

    def is_https(self, request) -> bool:
        """True when the browser's own hop was TLS, for the Secure cookie flag."""
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return False
        return request.headers.get("x-forwarded-proto", "").lower() == "https"

    # --- accounting ---

    def _prune(self, now: float) -> None:
        for key, (_, last) in list(self._failures.items()):
            if now - last > BACKOFF_PRUNE_SECONDS:
                del self._failures[key]
        for key, stamps in list(self._budget.items()):
            while stamps and now - stamps[0] > BUDGET_WINDOW_SECONDS:
                stamps.popleft()
            if not stamps:
                del self._budget[key]

    @staticmethod
    def _delay(failures: int) -> float:
        return min(2.0 ** (failures - 1), BACKOFF_CAP_SECONDS)

    def check(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> float | None:
        """Seconds still to wait before another attempt, or None if allowed."""
        now = self._clock()
        self._prune(now)
        waits: list[float] = []

        entry = self._failures.get((kind, subject, client))
        if entry:
            failures, last = entry
            waits.append(last + self._delay(failures) - now)

        if not trusted:
            stamps = self._budget.get((kind, subject))
            if stamps and len(stamps) >= BUDGET_THRESHOLD:
                over = len(stamps) - BUDGET_THRESHOLD + 1
                waits.append(stamps[-1] + self._delay(over) - now)

        remaining = max(waits, default=0.0)
        return remaining if remaining > 0 else None

    def record_failure(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        now = self._clock()
        self._prune(now)
        failures, _ = self._failures.get((kind, subject, client), (0, 0.0))
        self._failures[(kind, subject, client)] = (failures + 1, now)
        if not trusted:
            self._budget.setdefault((kind, subject), deque()).append(now)

    def record_success(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        """Clear this key's counter.

        The per-user budget is deliberately left alone: one correct guess must
        not wipe the cost an attack has already accumulated.
        """
        self._prune(self._clock())
        self._failures.pop((kind, subject, client), None)
