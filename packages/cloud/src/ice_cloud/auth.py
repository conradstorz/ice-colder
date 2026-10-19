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
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from ice_cloud.portal import VIEWER
from ice_cloud.settings import CloudSettings
from ice_cloud.store.repository import Account, EmailConflict, Machine
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud import signing
from ice_ui.levels import Level

SESSION_COOKIE = "ice_cloud_session"
OIDC_COOKIE = "ice_cloud_oidc"
IDLE_LIMIT = timedelta(days=30)
ABSOLUTE_LIMIT = timedelta(days=90)
REFRESH_EVERY = timedelta(hours=1)
OIDC_TTL = timedelta(minutes=10)

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

LEVEL_LOGIN = Level(title="Sign in", url="/login")

Role = Literal["owner", "tech", "admin"]


class SignInError(Exception):
    """Sign-in could not be verified; the message is safe to show the user."""


@dataclass(frozen=True)
class IdentityClaims:
    sub: str
    email: str
    email_verified: bool
    name: str


class OidcProvider(Protocol):
    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    async def exchange(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> IdentityClaims: ...


# --------------------------------------------------------------------------- #
# Signed cookies
# --------------------------------------------------------------------------- #


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _seal(key: bytes, payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(mac)}"


def _unseal(key: bytes, value: str) -> dict | None:
    body, dot, tag = value.partition(".")
    if not dot:
        return None
    try:
        given = _b64d(tag)
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(given, expected):
            return None
        payload = json.loads(_b64d(body))
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


# --------------------------------------------------------------------------- #
# Google provider
# --------------------------------------------------------------------------- #


def _pkce_challenge(verifier: str) -> str:
    return _b64e(hashlib.sha256(verifier.encode("ascii")).digest())


class GoogleProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._clock = clock
        self._discovery: dict | None = None
        self._jwks: dict[str, dict] = {}

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GOOGLE_AUTHORIZE_URL}?{query}"

    async def _get_json(self, url: str) -> dict:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignInError("Google could not be reached") from exc
        if not isinstance(data, dict):
            raise SignInError("Google returned an unexpected response")
        return data

    async def _discover(self, *, refresh: bool = False) -> dict:
        if self._discovery is None or refresh:
            self._discovery = await self._get_json(GOOGLE_DISCOVERY_URL)
        return self._discovery

    async def _key_for(self, kid: str) -> dict:
        if kid not in self._jwks:
            await self._refresh_jwks()
        if kid not in self._jwks:
            await self._refresh_jwks(rediscover=True)
        if kid not in self._jwks:
            raise SignInError("Unknown signing key")
        return self._jwks[kid]

    async def _refresh_jwks(self, *, rediscover: bool = False) -> None:
        doc = await self._discover(refresh=rediscover)
        uri = doc.get("jwks_uri")
        if not isinstance(uri, str):
            raise SignInError("Google discovery is incomplete")
        keys = (await self._get_json(uri)).get("keys", [])
        self._jwks = {k["kid"]: k for k in keys if isinstance(k, dict) and "kid" in k}

    async def exchange(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> IdentityClaims:
        doc = await self._discover()
        endpoint = doc.get("token_endpoint")
        if not isinstance(endpoint, str):
            raise SignInError("Google discovery is incomplete")
        try:
            response = await self._http.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code_verifier": code_verifier,
                },
            )
            response.raise_for_status()
            id_token = response.json().get("id_token")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SignInError("Google refused the sign-in") from exc
        if not isinstance(id_token, str):
            raise SignInError("Google returned no ID token")
        return await self._verify(id_token, nonce)

    async def _verify(self, token: str, nonce: str) -> IdentityClaims:
        parts = token.split(".")
        if len(parts) != 3:
            raise SignInError("Malformed ID token")
        try:
            header = json.loads(_b64d(parts[0]))
            claims = json.loads(_b64d(parts[1]))
            signature = _b64d(parts[2])
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise SignInError("Malformed ID token") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise SignInError("Malformed ID token")
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise SignInError("Unsupported ID token")
        jwk = await self._key_for(header["kid"])
        try:
            public = RSAPublicNumbers(
                int.from_bytes(_b64d(jwk["e"]), "big"),
                int.from_bytes(_b64d(jwk["n"]), "big"),
            ).public_key()
            public.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, KeyError, ValueError, binascii.Error) as exc:
            raise SignInError("ID token signature is invalid") from exc

        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise SignInError("ID token issuer is wrong")
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self._client_id not in audiences:
            raise SignInError("ID token audience is wrong")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or exp <= self._clock():
            raise SignInError("ID token has expired")
        given = claims.get("nonce")
        if not isinstance(given, str) or not hmac.compare_digest(
            given.encode(), nonce.encode()
        ):
            raise SignInError("ID token nonce is wrong")
        verified = claims.get("email_verified")
        if verified is not True and verified != "true":
            raise SignInError("Your Google email address is not verified")
        sub, email = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not sub or not isinstance(email, str):
            raise SignInError("ID token is missing the account identity")
        name = claims.get("name")
        return IdentityClaims(
            sub=sub,
            email=email,
            email_verified=True,
            name=name if isinstance(name, str) and name else email,
        )


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
        role: Role | None = await asyncio.to_thread(
            repo.access_for, account.id, machine.fingerprint
        )
        if (
            role is None
            and not owner_only
            and is_admin(request.app.state.settings, account)
        ):
            role = "admin"
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
