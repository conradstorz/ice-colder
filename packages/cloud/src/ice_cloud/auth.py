"""People-side authentication: Google sign-in, signed sessions, access checks.

Sign-in is the OpenID Connect authorization-code flow with PKCE against Google
(identity spec 6.1). The ID token is verified here with ``cryptography`` (RS256);
there is no JWT library. A session is a signed cookie (6.2): ``base64url(json)``
plus ``.`` plus ``base64url(HMAC-SHA256)`` under ``ICE_CLOUD_SESSION_KEY``.

Access dependencies used by every portal route:

* :func:`current_account` - the signed-in, non-suspended account, else ``None``.
* :func:`require_signed_in` - the account, else :class:`SignInRequired`, which the
  handler installed by :func:`install` turns into ``303 /login?next=...``.
* :func:`require_machine_access` - a factory; the dependency it returns takes the
  ``short`` path parameter and yields ``(account, machine, role)``. A non-member
  gets 404 so they cannot learn that a machine exists.
* :func:`require_admin`, :func:`require_htmx` (the CSRF guard for mutating routes).

A refreshed session cookie (``last_active_at`` is rewritten at most once per
``REFRESH_EVERY``) is stashed on ``request.state`` by ``current_account`` and
written to the response by a small middleware, because a dependency cannot set
headers on a response object the route builds itself.
"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from ice_cloud.oidc import IdentityClaims, OidcProvider, SignInError, b64d, b64e
from ice_cloud.oidc_google import GoogleProvider
from ice_cloud.settings import CloudSettings
from ice_cloud.store.repository import Account, EmailConflict, Machine
from ice_cloud.viewer import VIEWER
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud import signing
from ice_ui.levels import Level

__all__ = [
    "GoogleProvider",
    "IdentityClaims",
    "OidcProvider",
    "SignInError",
]

SESSION_COOKIE = "ice_cloud_session"
OIDC_COOKIE = "ice_cloud_oidc"
IDLE_LIMIT = timedelta(days=30)
ABSOLUTE_LIMIT = timedelta(days=90)
REFRESH_EVERY = timedelta(hours=1)
OIDC_TTL = timedelta(minutes=10)

LEVEL_LOGIN = Level(title="Sign in", url="/login")

Role = Literal["owner", "tech", "admin"]


# --------------------------------------------------------------------------- #
# Signed cookies
# --------------------------------------------------------------------------- #


def _seal(key: bytes, payload: dict) -> str:
    body = b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{b64e(mac)}"


def decode_flow(key: bytes, value: str) -> dict | None:
    """The payload of a signed OIDC flow cookie, or ``None`` if forged or malformed."""
    return _unseal(key, value)


def _unseal(key: bytes, value: str) -> dict | None:
    body, dot, tag = value.partition(".")
    if not dot:
        return None
    try:
        given = b64d(tag)
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(given, expected):
            return None
        payload = json.loads(b64d(body))
    except (ValueError, binascii.Error, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class SessionData:
    account_id: str
    issued_at: datetime
    last_active_at: datetime


def encode_session(
    key: bytes, account_id: str, issued_at: datetime, last_active_at: datetime
) -> str:
    return _seal(
        key,
        {"a": account_id, "i": issued_at.timestamp(), "l": last_active_at.timestamp()},
    )


def decode_session(key: bytes, value: str, now: datetime) -> SessionData | None:
    payload = _unseal(key, value)
    if payload is None:
        return None
    try:
        account_id = payload["a"]
        issued = datetime.fromtimestamp(float(payload["i"]), UTC)
        active = datetime.fromtimestamp(float(payload["l"]), UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if not isinstance(account_id, str):
        return None
    if now - active > IDLE_LIMIT or now - issued > ABSOLUTE_LIMIT:
        return None
    return SessionData(account_id, issued, active)


def _pkce_challenge(verifier: str) -> str:
    return b64e(hashlib.sha256(verifier.encode("ascii")).digest())


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


class SignInRequired(HTTPException):
    """Raised by :func:`require_signed_in`; the handler redirects to /login."""

    def __init__(self) -> None:
        super().__init__(status_code=303)


def safe_next(value: str | None) -> str | None:
    """A same-site relative path, or ``None`` (never an open redirect)."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    if "\\" in value or any(ord(c) < 32 for c in value):
        return None
    return value


def require_htmx(request: Request) -> None:
    """CSRF guard: a cross-site form cannot add the ``HX-Request`` header."""
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=403, detail="HTMX request required")


def is_admin(settings: CloudSettings, account: Account) -> bool:
    return account.email.lower() in settings.admins


def _secure(settings: CloudSettings) -> bool:
    return not settings.insecure_cookies


def _set_session_cookie(response: Response, settings: CloudSettings, value: str):
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=int(ABSOLUTE_LIMIT.total_seconds()),
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
    )


async def current_account(request: Request) -> Account | None:
    state = request.app.state
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    now: datetime = state.now()
    session = decode_session(state.settings.session_key, cookie, now)
    if session is None:
        return None
    account = await asyncio.to_thread(state.repo.account, session.account_id)
    if account is None or account.suspended_at is not None:
        return None
    if now - session.last_active_at >= REFRESH_EVERY:
        request.state.refreshed_session = encode_session(
            state.settings.session_key, account.id, session.issued_at, now
        )
    return account


async def require_signed_in(
    account: Account | None = Depends(current_account),
) -> Account:
    if account is None:
        raise SignInRequired()
    return account


async def require_admin(
    request: Request, account: Account = Depends(require_signed_in)
) -> Account:
    if not is_admin(request.app.state.settings, account):
        raise HTTPException(status_code=404)
    return account


def machine_role(
    settings: CloudSettings, repo, account: Account, fingerprint: str
) -> Role | None:
    """The account's role on a machine: its membership, else ``admin`` for a
    platform admin, else ``None``."""
    role: Role | None = repo.access_for(account.id, fingerprint)
    if role is None and is_admin(settings, account):
        return "admin"
    return role


def require_machine_access(*, owner_only: bool = False):
    """Build a dependency yielding ``(account, machine, role)`` for ``{short}``.

    Role is ``owner``, ``tech`` or ``admin``. A platform admin passes when
    ``owner_only`` is false (and is reported as ``owner`` when also the owner);
    ``owner_only=True`` admits the machine's owner alone. Everything else,
    including an unknown machine, is 404.
    """

    async def dependency(
        short: str,
        request: Request,
        account: Account = Depends(require_signed_in),
    ) -> tuple[Account, Machine, Role]:
        repo = request.app.state.repo
        short16 = signing.parse_short_fingerprint(short)
        machine = (
            await asyncio.to_thread(repo.resolve_short, short16) if short16 else None
        )
        if machine is None:
            raise HTTPException(status_code=404)
        if owner_only:
            role: Role | None = await asyncio.to_thread(
                repo.access_for, account.id, machine.fingerprint
            )
        else:
            role = await asyncio.to_thread(
                machine_role,
                request.app.state.settings,
                repo,
                account,
                machine.fingerprint,
            )
        if role is None or (owner_only and role != "owner"):
            raise HTTPException(status_code=404)
        return account, machine, role

    return dependency


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

router = APIRouter()


def _login_page(
    request: Request, *, error: str | None = None, status: int = 200
) -> Response:
    registry = request.app.state.registry
    ui = registry.resolve(registry.installed_digest)
    settings: CloudSettings = request.app.state.settings
    context = {
        "request": request,
        "current_user": VIEWER,
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": LEVEL_LOGIN,
        "configured": settings.google_configured,
        "next": safe_next(request.query_params.get("next")),
        "error": error,
    }
    return ui.templates.TemplateResponse(
        request, "login.html", context, status_code=status
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _login_page(request)


@router.get("/auth/google/start")
async def google_start(request: Request, next: str | None = None):
    state = request.app.state
    provider: OidcProvider | None = state.provider
    if provider is None:
        return RedirectResponse("/login", status_code=303)
    flow_state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    now: datetime = state.now()
    sealed = _seal(
        state.settings.session_key,
        {
            "s": flow_state,
            "n": nonce,
            "v": verifier,
            "x": (now + OIDC_TTL).timestamp(),
            "next": safe_next(next),
        },
    )
    url = provider.authorize_url(
        state=flow_state, nonce=nonce, code_challenge=_pkce_challenge(verifier)
    )
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        OIDC_COOKIE,
        sealed,
        max_age=int(OIDC_TTL.total_seconds()),
        httponly=True,
        secure=_secure(state.settings),
        samesite="lax",
    )
    return response


def _fail(request: Request, message: str, status: int = 400) -> Response:
    response = _login_page(request, error=message, status=status)
    response.delete_cookie(OIDC_COOKIE)
    return response


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    app_state = request.app.state
    provider: OidcProvider | None = app_state.provider
    if provider is None:
        return RedirectResponse("/login", status_code=303)
    if error:
        return _fail(request, "Google sign-in was cancelled or refused.")
    now: datetime = app_state.now()
    sealed = request.cookies.get(OIDC_COOKIE)
    flow = _unseal(app_state.settings.session_key, sealed) if sealed else None
    if (
        flow is None
        or not isinstance(flow.get("x"), int | float)
        or flow["x"] < now.timestamp()
        or not state
        or not code
        or not hmac.compare_digest(str(flow.get("s", "")).encode(), state.encode())
    ):
        return _fail(request, "Sign-in could not be verified. Please try again.")
    try:
        claims = await provider.exchange(
            code=code, code_verifier=flow["v"], nonce=flow["n"]
        )
        if not claims.email_verified:
            raise SignInError("Your Google email address is not verified")
    except SignInError as exc:
        logger.warning("sign-in refused: {}", exc)
        return _fail(request, str(exc) or "Sign-in failed.")
    try:
        account = await asyncio.to_thread(
            app_state.repo.upsert_account_from_login,
            claims.sub,
            claims.email,
            claims.name,
            now,
        )
    except EmailConflict:
        return _fail(
            request,
            "That email address already belongs to a different Google account.",
            status=409,
        )
    if account.suspended_at is not None:
        return _fail(request, "This account is suspended.", status=403)
    target = safe_next(flow.get("next")) or "/fleet"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(OIDC_COOKIE)
    _set_session_cookie(
        response,
        app_state.settings,
        encode_session(app_state.settings.session_key, account.id, now, now),
    )
    return response


@router.post("/logout", dependencies=[Depends(require_htmx)])
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def install(app: FastAPI) -> None:
    """Register the sign-in redirect handler and the session-refresh middleware."""

    async def redirect_to_login(request: Request, _exc: Exception) -> Response:
        target = "/login"
        if request.method == "GET":
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query
            target += "?" + urlencode({"next": path})
        return RedirectResponse(target, status_code=303)

    app.add_exception_handler(SignInRequired, redirect_to_login)

    @app.middleware("http")
    async def refresh_session(request: Request, call_next):
        response = await call_next(request)
        value = getattr(request.state, "refreshed_session", None)
        if value is not None:
            _set_session_cookie(response, request.app.state.settings, value)
        return response
