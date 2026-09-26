"""Session auth for the dashboard: cookies, session resolution, permissions.

The state lives in services/access.py's AccessStore; this module is the FastAPI
side of it. HTTP Basic auth is gone (Task 11) — a Backoff instance here
enforces per-subject rate-limiting with exponential back-off and per-user
budget for untrusted clients, and main.py hands it the trusted proxies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

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
