"""Named users, roles, PIN login, device trust and back-off for the dashboard.

Everything access-related lives here: the ``Backoff`` rate limiter, PIN and
code hashing, the permission table, and the ``AccessStore`` persisted to
``data/access.json``. That file holds secrets and PII and is never merged
into config.json.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from services.paths import DATA_DIR

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


ACCESS_FILE_NAME = "access.json"
DEVICE_PRUNE_HOURS = 24


def access_path() -> Path:
    """The access file location, resolved at call time so tests can patch DATA_DIR."""
    return DATA_DIR / ACCESS_FILE_NAME


class AccessError(Exception):
    """A refused access-store operation."""


class OwnerExistsError(AccessError):
    """At most one user may hold the owner role."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    id: str
    name: str
    email: str | None
    role: Role
    pin_hash: str
    pin_salt: str
    disabled: bool = False
    created_at: str = ""
    last_login_at: str | None = None


@dataclass
class Device:
    id: str
    token_hash: str
    label: str
    shared: bool = False
    trusted_user_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    last_seen_at: str = ""


class AccessStore:
    """Users, devices, sessions, codes and setup state for the dashboard.

    Persisted parts land in ``data/access.json`` atomically (tmp + rename)
    with mode 0600; sessions, OTPs and enrollment tokens live in memory only,
    so a restart merely means re-login.

    A file that will not parse sets ``corrupt``; every write then raises
    ``AccessError`` rather than overwriting whatever is there, and the
    dashboard serves an error page instead of an open setup wizard (spec §6).
    """

    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ):
        self._path = path if path is not None else access_path()
        self._clock = clock
        self._wall = wall_clock or _utc_now
        self.corrupt = False
        self.users: dict[str, User] = {}
        self.devices: dict[str, Device] = {}
        self._raw_extra: dict = {}
        self.load()

    # --- persistence ---

    @property
    def path(self) -> Path:
        return self._path

    def _stamp(self) -> str:
        return self._wall().isoformat()

    def load(self) -> None:
        self.users = {}
        self.devices = {}
        self._raw_extra = {}
        self.corrupt = False
        if not self._path.exists():
            return
        self._tighten_permissions()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("access file is not a JSON object")
            for uid, data in raw.get("users", {}).items():
                self.users[uid] = User(
                    id=uid,
                    name=data["name"],
                    email=data.get("email"),
                    role=Role(data["role"]),
                    pin_hash=data["pin_hash"],
                    pin_salt=data["pin_salt"],
                    disabled=bool(data.get("disabled", False)),
                    created_at=data.get("created_at", ""),
                    last_login_at=data.get("last_login_at"),
                )
            for did, data in raw.get("devices", {}).items():
                self.devices[did] = Device(
                    id=did,
                    token_hash=data["token_hash"],
                    label=data.get("label", ""),
                    shared=bool(data.get("shared", False)),
                    trusted_user_ids=[
                        u for u in data.get("trusted_user_ids", []) if u in self.users
                    ],
                    created_at=data.get("created_at", ""),
                    last_seen_at=data.get("last_seen_at", ""),
                )
            self._raw_extra = {
                k: v for k, v in raw.items() if k not in ("users", "devices")
            }
        except Exception as e:
            self.corrupt = True
            self.users = {}
            self.devices = {}
            logger.error(
                f"AccessStore: {self._path} is unreadable or invalid ({e}); the "
                "dashboard will serve an error page. The VMC and MQTT client "
                "are unaffected."
            )

    def _tighten_permissions(self) -> None:
        if os.name != "posix":
            return
        try:
            mode = self._path.stat().st_mode & 0o777
        except OSError:
            return
        if mode != 0o600:
            try:
                self._path.chmod(0o600)
                logger.warning(
                    f"AccessStore: {self._path} had mode {mode:04o}; tightened to 0600"
                )
            except OSError as e:
                logger.warning(f"AccessStore: could not chmod {self._path}: {e}")

    def _document(self) -> dict:
        doc = dict(self._raw_extra)
        doc["users"] = {
            u.id: {
                "name": u.name,
                "email": u.email,
                "role": u.role.value,
                "pin_hash": u.pin_hash,
                "pin_salt": u.pin_salt,
                "disabled": u.disabled,
                "created_at": u.created_at,
                "last_login_at": u.last_login_at,
            }
            for u in self.users.values()
        }
        doc["devices"] = {
            d.id: {
                "token_hash": d.token_hash,
                "label": d.label,
                "shared": d.shared,
                "trusted_user_ids": list(d.trusted_user_ids),
                "created_at": d.created_at,
                "last_seen_at": d.last_seen_at,
            }
            for d in self.devices.values()
        }
        return doc

    def save(self) -> None:
        """Atomic tmp + rename. The temp file is opened 0600 so neither it nor
        the final file is ever world-readable regardless of umask."""
        if self.corrupt:
            raise AccessError("access file is corrupt; refusing to overwrite it")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._document(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self._tighten_permissions()

    # --- users ---

    def owner(self) -> User | None:
        return next((u for u in self.users.values() if u.role is Role.owner), None)

    def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def enabled_users(self) -> list[User]:
        return sorted(
            (u for u in self.users.values() if not u.disabled), key=lambda u: u.name
        )

    def create_user(self, name: str, email: str | None, role: Role, pin: str) -> User:
        role = Role(role)
        if role is Role.owner and self.owner() is not None:
            raise OwnerExistsError("this machine already has an owner")
        pin_hash, pin_salt = hash_pin(pin)
        user = User(
            id=str(uuid.uuid4()),
            name=name,
            email=email or None,
            role=role,
            pin_hash=pin_hash,
            pin_salt=pin_salt,
            created_at=self._stamp(),
        )
        self.users[user.id] = user
        try:
            self.save()
        except Exception:
            del self.users[user.id]
            raise
        logger.info(f"AccessStore: created user {name} ({role.value})")
        return user

    def update_user(
        self,
        user_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        role: Role | None = None,
    ) -> User:
        user = self._require_user(user_id)
        if role is not None:
            role = Role(role)
            current_owner = self.owner()
            if (
                role is Role.owner
                and current_owner is not None
                and current_owner.id != user_id
            ):
                raise OwnerExistsError("this machine already has an owner")
            user.role = role
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email or None
        self.save()
        return user

    def set_user_pin(self, user_id: str, pin: str) -> None:
        """New PIN, and the user is dropped from every device so the next
        login re-enrolls (spec §4.1)."""
        user = self._require_user(user_id)
        user.pin_hash, user.pin_salt = hash_pin(pin)
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self.save()

    def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        self._require_user(user_id).disabled = bool(disabled)
        self.save()

    def delete_user(self, user_id: str) -> None:
        self._require_user(user_id)
        del self.users[user_id]
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self.save()

    def verify_user_pin(self, user_id: str, pin: str) -> bool:
        user = self.users.get(user_id)
        if user is None or user.disabled:
            return False
        return verify_pin(pin, user.pin_hash, user.pin_salt)

    def record_login(self, user_id: str) -> None:
        user = self._require_user(user_id)
        user.last_login_at = self._stamp()
        self.save()

    def _require_user(self, user_id: str) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise AccessError(f"no such user: {user_id}")
        return user

    # --- devices ---

    def create_device(self, label: str, shared: bool) -> tuple[Device, str]:
        """Return the record and the raw cookie token (never stored)."""
        token = generate_token()
        device = Device(
            id=str(uuid.uuid4()),
            token_hash=token_fingerprint(token),
            label=label,
            shared=bool(shared),
            created_at=self._stamp(),
            last_seen_at=self._stamp(),
        )
        self.devices[device.id] = device
        self.save()
        return device, token

    def device_for_token(self, token: str | None) -> Device | None:
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        return next(
            (d for d in self.devices.values() if d.token_hash == fingerprint), None
        )

    def trust_device(self, device_id: str, user_id: str) -> None:
        device = self._require_device(device_id)
        self._require_user(user_id)
        if user_id not in device.trusted_user_ids:
            device.trusted_user_ids.append(user_id)
        device.last_seen_at = self._stamp()
        self.save()

    def forget_device(self, device_id: str) -> None:
        self._require_device(device_id)
        del self.devices[device_id]
        self.save()

    def set_device_shared(self, device_id: str, shared: bool) -> None:
        self._require_device(device_id).shared = bool(shared)
        self.save()

    def touch_device(self, device_id: str) -> None:
        device = self.devices.get(device_id)
        if device is None:
            return
        device.last_seen_at = self._stamp()
        self.save()

    def prune_devices(self) -> int:
        """Drop devices older than 24 h that never completed enrollment."""
        cutoff = self._wall() - timedelta(hours=DEVICE_PRUNE_HOURS)
        removed = 0
        for device in list(self.devices.values()):
            if device.trusted_user_ids:
                continue
            try:
                created = datetime.fromisoformat(device.created_at)
                stale = created < cutoff
            except (ValueError, TypeError):
                continue
            if stale:
                del self.devices[device.id]
                removed += 1
        if removed:
            self.save()
        return removed

    def _require_device(self, device_id: str) -> Device:
        device = self.devices.get(device_id)
        if device is None:
            raise AccessError(f"no such device: {device_id}")
        return device
