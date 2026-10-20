"""Named users, roles, PIN login, device trust and back-off for the dashboard.

Everything access-related lives here: PIN and code hashing
(the ``Backoff`` rate limiter is ``ice_common.backoff``, imported here), the permission table, and the ``AccessStore`` persisted to
``data/access.json``. That file holds secrets and PII and is never merged
into config.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from loguru import logger

from ice_common.backoff import (  # noqa: F401  (re-exported for existing importers)
    BACKOFF_CAP_SECONDS,
    BACKOFF_PRUNE_SECONDS,
    BUDGET_THRESHOLD,
    BUDGET_WINDOW_SECONDS,
    Backoff,
)
from ice_common.observers import Subscribers
from ice_common.secrets import (  # noqa: F401  (re-exported for existing importers)
    SCRYPT_DKLEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    _scrypt,
    hash_secret,
    verify_secret,
)
from ice_common.paths import DATA_DIR
from ice_contracts.access import (  # noqa: F401  (re-exported for existing importers)
    ROLE_PERMISSIONS,
    Permission,
    Role,
)

EMERGENCY_CODE_COUNT = 20
CODE_DIGITS = 8
TRANSFER_TTL_DAYS = 7
LINK_TTL_DAYS = 7


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


# Fixed dummy hash+salt for verify_user_pin's miss path (unknown/disabled
# user), generated once at import so every miss pays the same scrypt cost a
# real wrong-PIN check would, instead of returning early and leaking which
# case it was through response timing.
_DUMMY_PIN_HASH, _DUMMY_PIN_SALT = hash_pin(secrets.token_hex(16))


def generate_code(digits: int) -> str:
    """A zero-padded random decimal code of exactly *digits* digits."""
    return str(secrets.randbelow(10**digits)).zfill(digits)


def generate_token() -> str:
    """A random 256-bit URL-safe token for cookies and session ids."""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """sha256 of a cookie value — what the store keeps instead of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


SHARED_IDLE_SECONDS = 300.0
PERSONAL_IDLE_SECONDS = 28800.0
SESSION_MAX_SECONDS = 86400.0
OTP_TTL_SECONDS = 600.0
OTP_DIGITS = 6
ENROLL_TTL_SECONDS = 600.0


@dataclass
class Session:
    id: str
    user_id: str
    device_id: str
    created_at: float
    last_active_at: float


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
        self._sessions: dict[str, Session] = {}
        # (user_id, device_id) -> (code, expires_at)
        self._pending_otps: dict[tuple[str, str], tuple[str, float]] = {}
        # enroll token -> (user_id, client, expires_at)
        self._enroll_tokens: dict[str, tuple[str, str, float]] = {}
        self._raw_extra: dict = {}
        self._claim_code_subscribers: Subscribers[[]] = Subscribers()
        self._setup_plaintext: str | None = None
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
        self._emergency_codes: list[dict] = []
        self._setup: dict = {}
        self._pending_transfer: dict | None = None
        self._pending_link: dict | None = None
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
            self._emergency_codes = list(raw.get("emergency_codes", []))
            self._setup = dict(raw.get("setup", {}))
            self._pending_transfer = raw.get("pending_transfer") or None
            self._pending_link = raw.get("pending_link") or None
            self._raw_extra = {
                k: v
                for k, v in raw.items()
                if k
                not in (
                    "users",
                    "devices",
                    "emergency_codes",
                    "setup",
                    "pending_transfer",
                    "pending_link",
                )
            }
        except Exception as e:
            self.corrupt = True
            self.users = {}
            self.devices = {}
            self._emergency_codes = []
            self._setup = {}
            self._pending_transfer = None
            self._pending_link = None
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
        doc["emergency_codes"] = [dict(c) for c in self._emergency_codes]
        doc["setup"] = dict(self._setup)
        doc["pending_transfer"] = self._pending_transfer
        doc["pending_link"] = self._pending_link
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

    def _commit(self) -> None:
        """Persist and re-raise on failure, after resyncing memory to disk.

        Every mutator writes to ``self.users``/``self.devices`` in memory
        before calling this, so a failed ``save()`` would otherwise leave
        memory ahead of what actually landed on disk until the next
        successful write. ``load()`` puts memory back in sync with disk
        first; the exception still propagates so the caller knows the write
        failed.
        """
        try:
            self.save()
        except Exception:
            self.load()
            raise

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
        self._commit()
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
        self._commit()
        return user

    def set_user_pin(self, user_id: str, pin: str) -> None:
        """New PIN, and the user is dropped from every device so the next
        login re-enrolls (spec §4.1)."""
        user = self._require_user(user_id)
        user.pin_hash, user.pin_salt = hash_pin(pin)
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self._commit()

    def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        self._require_user(user_id).disabled = bool(disabled)
        self._commit()

    def delete_user(self, user_id: str) -> None:
        self._require_user(user_id)
        del self.users[user_id]
        for device in self.devices.values():
            if user_id in device.trusted_user_ids:
                device.trusted_user_ids.remove(user_id)
        self._commit()

    def verify_user_pin(self, user_id: str, pin: str) -> bool:
        user = self.users.get(user_id)
        if user is None or user.disabled:
            # Pay the same scrypt cost a real wrong-PIN check would, so an
            # unknown or disabled user id can't be distinguished from a
            # wrong PIN by response timing.
            verify_pin(pin, _DUMMY_PIN_HASH, _DUMMY_PIN_SALT)
            return False
        return verify_pin(pin, user.pin_hash, user.pin_salt)

    def record_login(self, user_id: str) -> None:
        user = self._require_user(user_id)
        user.last_login_at = self._stamp()
        self._commit()

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
        self._commit()
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
        self._commit()

    def forget_device(self, device_id: str) -> None:
        self._require_device(device_id)
        del self.devices[device_id]
        self._commit()

    def set_device_shared(self, device_id: str, shared: bool) -> None:
        self._require_device(device_id).shared = bool(shared)
        self._commit()

    def touch_device(self, device_id: str) -> None:
        device = self.devices.get(device_id)
        if device is None:
            return
        device.last_seen_at = self._stamp()
        self._commit()

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
            self._commit()
        return removed

    def _require_device(self, device_id: str) -> Device:
        device = self.devices.get(device_id)
        if device is None:
            raise AccessError(f"no such device: {device_id}")
        return device

    # --- sessions (memory only) ---

    def create_session(self, user_id: str, device_id: str) -> str:
        now = self._clock()
        session = Session(
            id=generate_token(),
            user_id=user_id,
            device_id=device_id,
            created_at=now,
            last_active_at=now,
        )
        self._sessions[session.id] = session
        return session.id

    def _session_still_valid(self, session: Session) -> bool:
        """Whether ``session`` is still live right now, per spec §6.

        Checks user/device existence, the user's disabled flag, the
        device-kind idle limit and the absolute session cap. Reads
        ``self._clock()`` but never mutates ``session`` or ``self._sessions``
        — callers decide what to do with an invalid session (forget it, or
        just report it as not live).
        """
        device = self.devices.get(session.device_id)
        user = self.users.get(session.user_id)
        if device is None or user is None or user.disabled:
            return False
        now = self._clock()
        idle_limit = SHARED_IDLE_SECONDS if device.shared else PERSONAL_IDLE_SECONDS
        if (
            now - session.last_active_at > idle_limit
            or now - session.created_at > SESSION_MAX_SECONDS
        ):
            return False
        return True

    def resolve_session(
        self, session_id: str | None, *, touch: bool = True
    ) -> Session | None:
        """The live session for this cookie, refreshing its idle clock.

        Returns None — and forgets the session — when it has idled out, hit
        the absolute cap, or lost its user or device (spec §6).

        ``touch=False`` applies the exact same validity rules (and still
        forgets an expired session) but does not update ``last_active_at``.
        Dashboard v2's polling fragments (``/status``, ``/kpi``, ``/pill`` —
        see ``ice_edge/web/auth.POLLING_PATHS``) resolve sessions with
        ``touch=False`` so an open tab merely polling for updates does not
        count as activity and cannot keep a session — or a standby
        maintenance lease bound to it — alive indefinitely.
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if not self._session_still_valid(session):
            del self._sessions[session_id]
            return None
        if touch:
            session.last_active_at = self._clock()
        return session

    def session_is_live(self, session_id: str | None) -> bool:
        """Whether ``session_id`` would resolve right now, without side effects.

        Applies the same rules as ``resolve_session`` (unknown id, missing
        user or device, disabled user, idle limit, absolute cap) but never
        touches ``last_active_at`` and never deletes an expired entry —
        ``resolve_session`` remains the one place a session is forgotten.
        Used by the VMC's standby-lease sweep (spec §2.2a) to check whether
        the lease holder's session is still around without refreshing it.
        """
        if not session_id:
            return False
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return self._session_still_valid(session)

    def end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def end_all_sessions(self) -> None:
        self._sessions.clear()

    def end_sessions_for_user(self, user_id: str) -> None:
        for sid, session in list(self._sessions.items()):
            if session.user_id == user_id:
                del self._sessions[sid]

    def end_sessions_for_device(self, device_id: str) -> None:
        for sid, session in list(self._sessions.items()):
            if session.device_id == device_id:
                del self._sessions[sid]

    # --- one-time passwords (memory only) ---

    def issue_otp(self, user_id: str, device_id: str) -> str:
        """A fresh 6-digit OTP for this (user, device), replacing any pending one."""
        code = generate_code(OTP_DIGITS)
        self._pending_otps[(user_id, device_id)] = (
            code,
            self._clock() + OTP_TTL_SECONDS,
        )
        return code

    def verify_otp(self, user_id: str, device_id: str, code: str) -> bool:
        entry = self._pending_otps.get((user_id, device_id))
        if entry is None:
            return False
        stored, expires_at = entry
        if self._clock() > expires_at:
            del self._pending_otps[(user_id, device_id)]
            return False
        if not secrets.compare_digest(stored, code):
            return False
        del self._pending_otps[(user_id, device_id)]
        return True

    # --- enrollment tokens (memory only) ---

    def issue_enroll_token(self, user_id: str, client: str) -> str:
        """Proof that this client just verified *user_id*'s PIN, valid 10 minutes."""
        token = generate_token()
        self._enroll_tokens[token] = (
            user_id,
            client,
            self._clock() + ENROLL_TTL_SECONDS,
        )
        return token

    def resolve_enroll_token(self, token: str | None, client: str) -> str | None:
        if not token:
            return None
        entry = self._enroll_tokens.get(token)
        if entry is None:
            return None
        user_id, bound_client, expires_at = entry
        if self._clock() > expires_at:
            del self._enroll_tokens[token]
            return None
        if bound_client != client:
            return None
        return user_id

    def clear_enroll_token(self, token: str) -> None:
        self._enroll_tokens.pop(token, None)

    # --- emergency codes ---

    def generate_emergency_codes(self, count: int = EMERGENCY_CODE_COUNT) -> list[str]:
        """Replace the whole pool, used or not. Plaintexts are returned once."""
        codes: list[str] = []
        while len(codes) < count:
            code = generate_code(CODE_DIGITS)
            if code not in codes:
                codes.append(code)
        self._emergency_codes = [
            {
                "code_hash": hash_secret(c),
                "used_at": None,
                "used_by_user_id": None,
                "used_for": None,
            }
            for c in codes
        ]
        self._commit()
        logger.info(f"AccessStore: generated {count} emergency codes")
        return codes

    def unused_emergency_code_count(self) -> int:
        return sum(1 for c in self._emergency_codes if c["used_at"] is None)

    def consume_emergency_code(self, code: str, user_id: str, used_for: str) -> bool:
        """Mark an unused code used. False when it is unknown or already spent."""
        for entry in self._emergency_codes:
            if entry["used_at"] is None and verify_secret(code, entry["code_hash"]):
                entry["used_at"] = self._stamp()
                entry["used_by_user_id"] = user_id
                entry["used_for"] = used_for
                self._commit()
                return True
        return False

    # --- setup code ---

    @property
    def setup_mode(self) -> bool:
        return self.owner() is None

    @property
    def setup_finalized(self) -> bool:
        return bool(self._setup.get("finalized"))

    @property
    def pending_setup_code(self) -> str | None:
        """The plaintext, held in memory only while setup is unfinished."""
        return self._setup_plaintext

    def begin_setup(self) -> str:
        """Generate (or return) the setup code that unlocks the wizard.

        Only someone at the machine — reading the startup log or the customer
        display — can see it, so a remote stranger cannot claim the machine.
        """
        if self.setup_finalized:
            raise AccessError("setup has already been finalized")
        if self._setup_plaintext and self._setup.get("setup_code_hash"):
            return self._setup_plaintext
        code = generate_code(CODE_DIGITS)
        self._setup = {"setup_code_hash": hash_secret(code), "finalized": False}
        self._commit()
        self._setup_plaintext = code
        self._claim_code_subscribers.publish()
        return code

    def verify_setup_code(self, code: str) -> bool:
        stored = self._setup.get("setup_code_hash")
        if not stored or self.setup_finalized:
            return False
        return verify_secret(code, stored)

    def finalize_setup(self) -> None:
        self._setup = {"setup_code_hash": None, "finalized": True}
        self._setup_plaintext = None
        self._commit()
        self._claim_code_subscribers.publish()

    # --- ownership transfer ---

    @property
    def pending_transfer(self) -> dict | None:
        """The live transfer, or None once it has expired."""
        pending = self._pending_transfer
        if pending is None:
            return None
        try:
            expires = datetime.fromisoformat(pending["expires_at"])
        except (KeyError, ValueError):
            return None
        if self._wall() > expires:
            return None
        return pending

    def start_transfer(self, started_by_user_id: str) -> str:
        """Record a pending transfer and return its 8-digit code, shown once.

        Nothing else changes: the current owner stays in control until the
        incoming owner completes the wizard (spec §3.3).
        """
        self._require_user(started_by_user_id)
        code = generate_code(CODE_DIGITS)
        now = self._wall()
        self._pending_link = None
        self._pending_transfer = {
            "transfer_code_hash": hash_secret(code),
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(days=TRANSFER_TTL_DAYS)).isoformat(),
            "started_by_user_id": started_by_user_id,
        }
        self._commit()
        self._claim_code_subscribers.publish()
        logger.warning("AccessStore: ownership transfer started")
        return code

    def verify_transfer_code(self, code: str) -> bool:
        pending = self.pending_transfer
        if pending is None:
            return False
        return verify_secret(code, pending["transfer_code_hash"])

    def cancel_transfer(self) -> None:
        self._pending_transfer = None
        self._commit()
        self._claim_code_subscribers.publish()

    def complete_transfer(self, name: str, email: str | None, pin: str) -> User:
        """Swap the owner in one write.

        Creates the new owner, deletes the old one (and their device trust),
        deletes every emergency code, ends every session, and clears the
        pending transfer. Other users are retained for the review step.

        Spec §3.3 step 4: the transfer code remains valid as an enrollment
        code for the incoming owner until Done. Before clearing
        ``_pending_transfer`` its hash is moved into ``setup`` as
        ``{"setup_code_hash": <hash>, "finalized": False}`` —
        ``verify_setup_code`` already refuses once ``finalized`` is true, so
        a later ``finalize_setup()`` (Task 15's Done) closes the window.
        ``pending_transfer`` itself is not kept alive to achieve this: a
        live pending transfer would leave the machine claimable by anyone
        who still held the code.
        """
        pending = self.pending_transfer
        if pending is None:
            raise AccessError("no pending ownership transfer")
        old_owner = self.owner()
        pin_hash, pin_salt = hash_pin(pin)
        new_owner = User(
            id=str(uuid.uuid4()),
            name=name,
            email=email or None,
            role=Role.owner,
            pin_hash=pin_hash,
            pin_salt=pin_salt,
            created_at=self._stamp(),
        )
        if old_owner is not None:
            del self.users[old_owner.id]
            for device in self.devices.values():
                if old_owner.id in device.trusted_user_ids:
                    device.trusted_user_ids.remove(old_owner.id)
        self.users[new_owner.id] = new_owner
        self._emergency_codes = []
        self._setup = {
            "setup_code_hash": pending["transfer_code_hash"],
            "finalized": False,
        }
        self._pending_transfer = None
        self._commit()
        self._claim_code_subscribers.publish()
        self.end_all_sessions()
        logger.warning(f"AccessStore: ownership transferred to {name}")
        return new_owner

    # --- link to cloud ---

    @property
    def pending_link(self) -> dict | None:
        """The live link code record, or None once it has expired."""
        pending = self._pending_link
        if pending is None:
            return None
        try:
            expires = datetime.fromisoformat(pending["expires_at"])
        except (KeyError, ValueError):
            return None
        if self._wall() > expires:
            return None
        return pending

    def start_link(self, started_by_user_id: str) -> str:
        """Mint a link-to-cloud code (8 digits, shown once, 7-day expiry)."""
        self._require_user(started_by_user_id)
        if self.pending_transfer is not None:
            raise AccessError("an ownership transfer is pending")
        code = generate_code(CODE_DIGITS)
        now = self._wall()
        self._pending_link = {
            "code_hash": hash_secret(code),
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(days=LINK_TTL_DAYS)).isoformat(),
        }
        self._commit()
        self._claim_code_subscribers.publish()
        return code

    def cancel_link(self) -> None:
        self._pending_link = None
        self._commit()
        self._claim_code_subscribers.publish()

    # --- cloud claim code ---

    def cloud_claim_code(self) -> tuple[str, str | None] | None:
        """The live claim code as (hash, expires_at ISO or None for no expiry).

        Priority: a live transfer, else a live link, else the unfinalised
        setup code.
        """
        transfer = self.pending_transfer
        if transfer is not None:
            return transfer["transfer_code_hash"], transfer["expires_at"]
        link = self.pending_link
        if link is not None:
            return link["code_hash"], link["expires_at"]
        setup_hash = self._setup.get("setup_code_hash")
        if setup_hash and not self.setup_finalized:
            return setup_hash, None
        return None

    def subscribe_claim_code_change(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call ``cb`` (no payload; read ``cloud_claim_code()``) after each change."""
        return self._claim_code_subscribers.subscribe(cb)

    # --- machine report ---

    def machine_report(self, config) -> str:
        """Plain-text summary of who can reach this machine (spec §3.4)."""
        owner = self.owner()
        lines = [
            "Ice-Colder machine access report",
            "================================",
            "",
            f"Machine id:   {config.machine_id}",
            f"Machine name: {config.physical.common_name}",
            f"Owner:        {owner.name if owner else '(none)'}"
            f" <{owner.email if owner and owner.email else 'no email'}>",
            "",
            "Users",
            "-----",
        ]
        for user in sorted(self.users.values(), key=lambda u: u.name):
            devices = sum(
                1 for d in self.devices.values() if user.id in d.trusted_user_ids
            )
            lines.append(
                f"  {user.name} ({user.role.value})"
                f" email={user.email or '-'}"
                f" disabled={user.disabled}"
                f" last_login={user.last_login_at or 'never'}"
                f" devices={devices}"
            )
        lines += ["", "Devices", "-------"]
        for device in sorted(self.devices.values(), key=lambda d: d.label):
            names = ", ".join(
                self.users[uid].name
                for uid in device.trusted_user_ids
                if uid in self.users
            )
            lines.append(
                f"  {device.label} shared={device.shared}"
                f" trusted=[{names}] last_seen={device.last_seen_at}"
            )
        lines += [
            "",
            f"Unused emergency codes: {self.unused_emergency_code_count()}",
        ]
        return "\n".join(lines)
