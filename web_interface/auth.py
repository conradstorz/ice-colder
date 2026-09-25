"""Session auth for the dashboard: cookies, session resolution, permissions.

The state lives in services/access.py's AccessStore; this module is the FastAPI
side of it. HTTP Basic auth goes away in Task 11, and with it the LoginLimiter
below — a Backoff instance here replaces the limiter and main.py hands it the
trusted proxies.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import HTTPException, Request
from loguru import logger

from services.access import (
    ROLE_PERMISSIONS,
    AccessStore,
    Backoff,
    Permission,
    Role,
    Session,
    User,
)

DEVICE_COOKIE = "vmc_device"
SESSION_COOKIE = "vmc_session"
ENROLL_COOKIE = "vmc_enroll"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600
ENROLL_COOKIE_MAX_AGE = 600

backoff = Backoff()

_store: Optional[AccessStore] = None


def set_access_store(store: Optional[AccessStore]) -> None:
    global _store
    _store = store


def get_access_store() -> Optional[AccessStore]:
    return _store


# --- cookies ---


def set_cookie(
    response,
    request: Request,
    name: str,
    value: str,
    *,
    max_age: int | None,
    backoff: Backoff = backoff,
) -> None:
    """HttpOnly, SameSite=Lax, Path=/; Secure only when the browser hop was TLS."""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
        secure=backoff.is_https(request),
    )


def clear_cookie(response, name: str) -> None:
    response.delete_cookie(key=name, path="/")


# --- identity ---


@dataclass
class Principal:
    user: User
    session: Session
    perms: frozenset[Permission]


def current_principal(request: Request) -> Principal | None:
    """Resolve vmc_session to a live user, or None."""
    store = _store
    if store is None or store.corrupt:
        return None
    session = store.resolve_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return None
    user = store.get_user(session.user_id)
    if user is None or user.disabled:
        return None
    return Principal(user=user, session=session, perms=ROLE_PERMISSIONS[user.role])


def client_key(request: Request) -> str:
    """The back-off client key: the stored device id, else the client IP.

    A cookie that does not resolve to a device record is ignored, so an
    attacker minting random cookie values gets one counter per IP, not one
    per cookie (spec §2.4).
    """
    store = _store
    if store is not None and not store.corrupt:
        device = store.device_for_token(request.cookies.get(DEVICE_COOKIE))
        if device is not None:
            return device.id
    return backoff.client_ip(request)


def is_trusted_client(request: Request, user_id: str) -> bool:
    """True when this browser's device record already trusts *user_id*."""
    store = _store
    if store is None or store.corrupt:
        return False
    device = store.device_for_token(request.cookies.get(DEVICE_COOKIE))
    return device is not None and user_id in device.trusted_user_ids


def _unauthenticated(request: Request) -> HTTPException:
    """303 to /login for a page; 401 + HX-Redirect so a partial swap navigates."""
    if request.headers.get("HX-Request") == "true":
        return HTTPException(
            status_code=401, detail="Login required", headers={"HX-Redirect": "/login"}
        )
    return HTTPException(
        status_code=303, detail="Login required", headers={"Location": "/login"}
    )


def require(*permissions: Permission):
    """FastAPI dependency: a live session holding every listed permission."""

    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal is None:
            raise _unauthenticated(request)
        if any(p not in principal.perms for p in permissions):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    return dependency


@dataclass(frozen=True)
class TemplateUser:
    """The template-facing view of a User: everything a template needs to
    greet, label or gate on a user, and nothing a template must never leak.

    User carries pin_hash/pin_salt (needed to verify a PIN, irrelevant to
    rendering a page); no template renders them today, but current_user
    reaches every template, so the hazard is live for every template task
    still to come. Narrowing the type here, once, is cheaper than trusting
    every future template author to remember not to touch those fields.
    """

    id: str
    name: str
    email: str | None
    role: Role
    disabled: bool
    last_login_at: str | None


def _template_user(user: User) -> TemplateUser:
    return TemplateUser(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        disabled=user.disabled,
        last_login_at=user.last_login_at,
    )


def template_context(request: Request, **extra) -> dict:
    """Every template gets current_user and perms, so it never renders a
    button the server would refuse. Server-side checks remain the authority.

    current_user is a TemplateUser, not the full User — see TemplateUser's
    docstring. Code that needs the real User (pin_hash/pin_salt included)
    should use current_principal(request).user instead.
    """
    principal = current_principal(request)
    ctx = {
        "request": request,
        "current_user": _template_user(principal.user) if principal else None,
        "perms": principal.perms if principal else frozenset(),
    }
    ctx.update(extra)
    return ctx


# Task 11 deletes LoginLimiter and its HTTP-Basic call sites, replacing them
# with the session/cookie auth above.
class LoginLimiter:
    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: float = 900.0,
        lockout_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._max = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(
                    f"LoginLimiter: ignoring invalid trusted proxy CIDR {cidr!r}"
                )

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to rate-limit on.

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

    # --- accounting ---

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for ip in list(self._failures):
            dq = self._failures[ip]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                del self._failures[ip]
        for ip in list(self._locked_until):
            if self._locked_until[ip] <= now:
                del self._locked_until[ip]

    def check(self, ip: str) -> float | None:
        """Seconds remaining in a lockout for ip, or None if allowed."""
        now = self._clock()
        self._prune(now)
        until = self._locked_until.get(ip)
        if until is None:
            return None
        return until - now

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        self._prune(now)
        dq = self._failures.setdefault(ip, deque())
        dq.append(now)
        if len(dq) >= self._max:
            self._locked_until[ip] = now + self._lockout
            del self._failures[ip]
            logger.warning(
                f"Dashboard: {ip} locked out for {self._lockout:.0f}s after "
                f"{self._max} failed logins"
            )

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
