"""Google sign-in, sessions and the access dependencies (identity spec 6.1-6.2)."""

import asyncio
import base64
import json
import re
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import Depends

from ice_cloud import auth
from ice_cloud.auth import (
    ABSOLUTE_LIMIT,
    IDLE_LIMIT,
    SESSION_COOKIE,
    GoogleProvider,
    IdentityClaims,
    SignInError,
    decode_flow,
    decode_session,
    encode_session,
    require_admin,
    require_htmx,
    require_machine_access,
    require_signed_in,
    safe_next,
)
from tests.cloud.conftest import login
from tests.cloud.reports import FP, FP_2, NOW

KEY = b"k" * 32
HTMX = {"HX-Request": "true"}


@pytest.fixture
def probes(cloud_app):
    """A few gated routes standing in for the portal pages Task 12 will gate."""
    before = len(cloud_app.router.routes)

    @cloud_app.get("/probe")
    async def probe(account=Depends(require_signed_in)):
        return {"email": account.email}

    @cloud_app.get("/probe-admin")
    async def probe_admin(account=Depends(require_admin)):
        return {"email": account.email}

    @cloud_app.get("/m/{short}/probe")
    async def probe_machine(access=Depends(require_machine_access())):
        return {"role": access[2], "fingerprint": access[1].fingerprint}

    @cloud_app.get("/m/{short}/owner-probe")
    async def probe_owner(access=Depends(require_machine_access(owner_only=True))):
        return {"role": access[2]}

    @cloud_app.post("/probe-post", dependencies=[Depends(require_htmx)])
    async def probe_post():
        return {"ok": True}

    # The portal's catch-all 404 is registered first; probes must precede it.
    routes = cloud_app.router.routes
    routes[:] = routes[before:] + routes[:before]


def session_cookie(cloud) -> str | None:
    return cloud.cookies.get(SESSION_COOKIE)


def test_login_page_says_not_configured_without_google(cloud):
    r = cloud.get("/login")
    assert r.status_code == 200
    assert "not configured" in r.text
    assert "/auth/google/start" not in r.text


def test_login_page_has_no_identity_chip(cloud):
    r = cloud.get("/login")
    assert r.status_code == 200
    assert 'href="/me"' not in r.text


def test_login_page_offers_button_when_configured(settings, clock, provider, tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ice_cloud.app import create_app

    configured = replace(
        settings,
        google_client_id="id",
        google_client_secret="secret",
        public_url="https://cloud.example.com",
    )
    with TestClient(create_app(configured, now=clock, provider=provider)) as c:
        r = c.get("/login?next=/fleet")
    assert "Sign in with Google" in r.text
    assert "/auth/google/start?next=/fleet" in r.text
    # The link leaves the site for accounts.google.com. Inside the shell's
    # hx-boost scope htmx would fetch it by XHR, follow the 303 to Google, fail
    # CORS and silently stay on /login, so the anchor must opt out of boosting.
    anchor = re.search(r'<a\s[^>]*href="/auth/google/start[^"]*"[^>]*>', r.text)
    assert anchor is not None
    assert 'hx-boost="false"' in anchor.group(0)


def test_sign_in_creates_account_then_updates_name_on_return(cloud, probes):
    first = login(cloud, "ann@example.com", sub="sub-ann", name="Ann")
    assert first.name == "Ann"
    assert cloud.get("/probe").json() == {"email": "ann@example.com"}

    second = login(cloud, "ann@example.com", sub="sub-ann", name="Ann B")
    assert second.id == first.id
    assert second.name == "Ann B"


def test_sign_in_lands_on_fleet_or_safe_next(cloud):
    cloud.app.state.provider.claims = IdentityClaims("s1", "a@example.com", True, "A")
    start = cloud.get("/auth/google/start?next=/m/abc")
    r = cloud.get(start.headers["location"])
    assert r.headers["location"] == "/m/abc"

    start = cloud.get("/auth/google/start?next=//evil.example.com")
    r = cloud.get(start.headers["location"])
    assert r.headers["location"] == "/fleet"


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example.com",
        "/\\evil.example.com",
        "javascript:alert(1)",
        "https://evil.example.com",
        "/fleet\nSet-Cookie: x=1",
        "/fleet\x00",
        "",
        None,
    ],
)
def test_safe_next_refuses_unsafe_values(value):
    assert safe_next(value) is None


@pytest.mark.parametrize("value", ["/fleet", "/m/abcd-abcd-abcd-abcd"])
def test_safe_next_accepts_local_paths(value):
    assert safe_next(value) == value


def test_login_page_renders_only_safe_next(settings, clock, provider):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ice_cloud.app import create_app

    configured = replace(
        settings,
        google_client_id="id",
        google_client_secret="s",
        public_url="https://c.example.com",
    )
    app = create_app(configured, now=clock, provider=provider)
    with TestClient(app, follow_redirects=False) as c:
        good = c.get("/login?next=/m/abcd-abcd-abcd-abcd").text
        assert "next=/m/abcd-abcd-abcd-abcd" in good
        bad = c.get("/login?next=//evil.example.com").text
        assert "evil.example.com" not in bad
        assert "/auth/google/start" in bad
        assert "next=" not in bad


def test_callback_passes_cookie_nonce_and_verifier_to_provider(cloud, provider):
    start = cloud.get("/auth/google/start")
    flow = decode_flow(
        cloud.app.state.settings.session_key, cloud.cookies["ice_cloud_oidc"]
    )
    assert cloud.get(start.headers["location"]).status_code == 303
    assert provider.exchanges == [
        {"code": "fake", "code_verifier": flow["v"], "nonce": flow["n"]}
    ]


def test_callback_clears_oidc_cookie_and_replay_fails(cloud, provider):
    start = cloud.get("/auth/google/start")
    first = cloud.get(start.headers["location"])
    assert first.status_code == 303
    assert "ice_cloud_oidc" not in cloud.cookies
    replay = cloud.get(start.headers["location"])
    assert replay.status_code == 400
    assert len(provider.exchanges) == 1


def test_logout_response_carries_no_refreshed_session_cookie(cloud, clock, probes):
    login(cloud, "r@example.com")
    clock.advance(2 * 3600)  # past REFRESH_EVERY, so the session is due a refresh
    r = cloud.post("/logout", headers=HTMX)
    assert r.status_code == 303
    cookies = r.headers.get_list("set-cookie")
    session = [c for c in cookies if c.startswith(SESSION_COOKIE + "=")]
    assert len(session) == 1
    assert "Max-Age=0" in session[0] or '=""' in session[0] or "1970" in session[0]


def test_unverified_email_refused(cloud, probes):
    cloud.app.state.provider.claims = IdentityClaims("s2", "b@example.com", False, "B")
    start = cloud.get("/auth/google/start")
    r = cloud.get(start.headers["location"])
    assert r.status_code == 400
    assert "not verified" in r.text
    assert session_cookie(cloud) is None
    assert cloud.app.state.repo.account_by_email("b@example.com") is None


def test_email_conflict_is_a_409_page(cloud):
    login(cloud, "c@example.com", sub="sub-one")
    cloud.cookies.clear()
    cloud.app.state.provider.claims = IdentityClaims(
        "sub-two", "c@example.com", True, "C"
    )
    start = cloud.get("/auth/google/start")
    r = cloud.get(start.headers["location"])
    assert r.status_code == 409
    assert session_cookie(cloud) is None


def test_bad_state_refused(cloud):
    cloud.get("/auth/google/start")
    r = cloud.get("/auth/google/callback?code=fake&state=not-the-state")
    assert r.status_code == 400
    assert session_cookie(cloud) is None


def test_callback_without_start_cookie_refused(cloud):
    r = cloud.get("/auth/google/callback?code=fake&state=anything")
    assert r.status_code == 400
    assert session_cookie(cloud) is None


def test_oidc_cookie_expires_after_ten_minutes(cloud, clock):
    start = cloud.get("/auth/google/start")
    clock.advance(11 * 60)
    r = cloud.get(start.headers["location"])
    assert r.status_code == 400
    assert session_cookie(cloud) is None


def test_session_tamper_is_signed_out(cloud, probes):
    login(cloud, "t@example.com")
    value = session_cookie(cloud)
    body, _, tag = value.partition(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["a"] = "someone-else"
    new_body = (
        base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    )
    for bad in (f"{new_body}.{tag}", f"{body}.AAAA", "garbage", value + "x"):
        cloud.cookies.set(SESSION_COOKIE, bad)
        assert cloud.get("/probe").status_code == 303


def test_session_signed_with_another_key_is_rejected():
    value = encode_session(b"a" * 32, "acct", NOW, NOW)
    assert decode_session(b"b" * 32, value, NOW) is None
    assert decode_session(b"a" * 32, value, NOW).account_id == "acct"


def test_idle_expiry(cloud, clock, probes):
    login(cloud, "i@example.com")
    clock.advance(IDLE_LIMIT.total_seconds() - 60)
    assert cloud.get("/probe").status_code == 200
    # activity refreshed the cookie, so one more near-limit gap is still fine
    clock.advance(2 * (IDLE_LIMIT.total_seconds() - 60))
    assert cloud.get("/probe").status_code == 200


def test_idle_limit_signs_out(cloud, clock, probes):
    login(cloud, "j@example.com")
    clock.advance(IDLE_LIMIT.total_seconds() + 60)
    assert cloud.get("/probe").status_code == 303


def test_absolute_expiry_despite_activity(cloud, clock, probes):
    login(cloud, "k@example.com")
    day = 24 * 3600
    for days in range(10, 90, 10):
        clock.advance(days * day)
        assert cloud.get("/probe").status_code == 200
    clock.advance(ABSOLUTE_LIMIT.total_seconds() + 60)
    assert cloud.get("/probe").status_code == 303


def test_cookie_refreshed_at_most_hourly(cloud, clock, probes):
    login(cloud, "r@example.com")
    first = session_cookie(cloud)
    clock.advance(600)
    r = cloud.get("/probe")
    assert "set-cookie" not in r.headers
    assert session_cookie(cloud) == first
    clock.advance(3700)
    r = cloud.get("/probe")
    assert SESSION_COOKIE in r.headers["set-cookie"]
    assert session_cookie(cloud) != first


def test_suspended_is_signed_out(cloud, clock, probes):
    account = login(cloud, "s@example.com")
    assert cloud.get("/probe").status_code == 200
    cloud.app.state.repo.set_suspended(account.id, clock())
    assert cloud.get("/probe").status_code == 303


def test_logout_requires_htmx_and_clears(cloud, probes):
    login(cloud, "l@example.com")
    assert cloud.post("/logout").status_code == 403
    assert cloud.get("/probe").status_code == 200
    r = cloud.post("/logout", headers=HTMX)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert session_cookie(cloud) is None
    assert cloud.get("/probe").status_code == 303


def test_signed_out_redirects_to_login_with_next(cloud, probes):
    r = cloud.get("/probe?x=1")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fprobe%3Fx%3D1"
    # a POST has no page to come back to
    assert cloud.post("/probe-post", headers=HTMX).status_code == 200
    assert cloud.post("/probe-post").status_code == 403


def test_session_cookie_flags(cloud):
    cloud.app.state.provider.claims = IdentityClaims("sf", "f@example.com", True, "F")
    start = cloud.get("/auth/google/start")
    header = cloud.get(start.headers["location"]).headers.get_list("set-cookie")
    session = next(h for h in header if h.startswith(SESSION_COOKIE))
    assert "HttpOnly" in session and "SameSite=lax" in session


def test_admin_dependency(cloud, probes):
    login(cloud, "someone@example.com")
    assert cloud.get("/probe-admin").status_code == 404
    login(cloud, "ADMIN@example.com")
    assert cloud.get("/probe-admin").status_code == 200


def _machine(cloud, fingerprint, owner=None):
    repo = cloud.app.state.repo
    repo.register_machine(fingerprint, "a2V5", "m-1", NOW)
    if owner is not None:
        assert repo.claim_machine(fingerprint, owner.id, NOW)


def test_machine_access_roles(cloud, probes):
    owner = login(cloud, "owner@example.com")
    tech = login(cloud, "tech@example.com")
    login(cloud, "stranger@example.com")
    admin = login(cloud, "admin@example.com")
    _machine(cloud, FP, owner)
    cloud.app.state.repo.grant_tech(tech.id, FP, owner.id, NOW)
    short = FP[:16]

    def get(path, email):
        login(cloud, email)
        return cloud.get(path)

    r = get(f"/m/{short}/probe", "owner@example.com")
    assert r.json() == {"role": "owner", "fingerprint": FP}
    assert get(f"/m/{short.upper()}/probe", "owner@example.com").status_code == 200
    assert get(f"/m/{short}/probe", "tech@example.com").json()["role"] == "tech"
    assert get(f"/m/{short}/probe", "admin@example.com").json()["role"] == "admin"
    assert get(f"/m/{short}/probe", "stranger@example.com").status_code == 404
    assert get(f"/m/{FP_2[:16]}/probe", "owner@example.com").status_code == 404
    assert get("/m/nothex/probe", "owner@example.com").status_code == 404
    assert admin.email == "admin@example.com"


def test_owner_only_excludes_tech_and_admin(cloud, probes):
    owner = login(cloud, "owner@example.com")
    tech = login(cloud, "tech@example.com")
    _machine(cloud, FP, owner)
    cloud.app.state.repo.grant_tech(tech.id, FP, owner.id, NOW)
    short = FP[:16]
    assert cloud.get(f"/m/{short}/owner-probe").status_code == 404  # tech signed in
    login(cloud, "admin@example.com")
    assert cloud.get(f"/m/{short}/owner-probe").status_code == 404
    login(cloud, "owner@example.com")
    assert cloud.get(f"/m/{short}/owner-probe").json() == {"role": "owner"}


def test_machine_access_signed_out_redirects(cloud, probes):
    assert cloud.get(f"/m/{FP[:16]}/probe").status_code == 303


# --------------------------------------------------------------------------- #
# GoogleProvider against a local RSA key and a mock transport
# --------------------------------------------------------------------------- #

CLIENT_ID = "client-123.apps.googleusercontent.com"
NONCE = "nonce-abc"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeGoogle:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.claims: dict = {}
        self.requests: list[str] = []
        self.token_forms: list[dict] = []
        self.id_token = ""
        self.keys_override: list | None = None

    def jwk(self, kid: str | None = None) -> dict:
        numbers = self.private.public_key().public_numbers()
        size = (numbers.n.bit_length() + 7) // 8
        return {
            "kty": "RSA",
            "alg": "RS256",
            "kid": kid or self.kid,
            "n": b64u(numbers.n.to_bytes(size, "big")),
            "e": b64u(numbers.e.to_bytes(3, "big")),
        }

    def mint(self, **overrides) -> str:
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "g-1",
            "email": "g@example.com",
            "email_verified": True,
            "name": "Gee",
            "nonce": NONCE,
            "exp": 2_000_000_000,
            **overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        head = b64u(json.dumps({"alg": "RS256", "kid": self.kid}).encode())
        body = b64u(json.dumps(claims).encode())
        sig = self.private.sign(
            f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{head}.{body}.{b64u(sig)}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                },
            )
        if request.url.path == "/oauth2/v3/certs":
            return httpx.Response(
                200, json={"keys": self.keys_override or [self.jwk()]}
            )
        if request.url.path == "/token":
            self.token_forms.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"id_token": self.id_token})
        return httpx.Response(404)


def make_provider(google: FakeGoogle) -> GoogleProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    return GoogleProvider(
        CLIENT_ID,
        "secret",
        "https://cloud.example.com/auth/google/callback",
        http=http,
        clock=lambda: 1_900_000_000.0,
    )


def exchange(provider: GoogleProvider, nonce: str = NONCE) -> IdentityClaims:
    return asyncio.run(provider.exchange(code="c", code_verifier="v", nonce=nonce))


def test_google_provider_verifies_rs256_and_rejects_wrong_aud_nonce_exp():
    google = FakeGoogle()
    provider = make_provider(google)

    google.id_token = google.mint()
    claims = exchange(provider)
    assert claims == IdentityClaims("g-1", "g@example.com", True, "Gee")
    form = google.token_forms[0]
    assert form["code"] == "c" and form["code_verifier"] == "v"
    assert form["client_id"] == CLIENT_ID

    # discovery and JWKS are cached across exchanges
    exchange(provider)
    assert google.requests.count("/.well-known/openid-configuration") == 1
    assert google.requests.count("/oauth2/v3/certs") == 1

    google.id_token = google.mint(aud="someone-else")
    with pytest.raises(SignInError, match="audience"):
        exchange(provider)
    google.id_token = google.mint(nonce="other")
    with pytest.raises(SignInError, match="nonce"):
        exchange(provider)
    google.id_token = google.mint(exp=1_800_000_000)
    with pytest.raises(SignInError, match="expired"):
        exchange(provider)
    google.id_token = google.mint(email_verified=False)
    with pytest.raises(SignInError, match="verified"):
        exchange(provider)
    google.id_token = google.mint(iss="https://evil.example.com")
    with pytest.raises(SignInError, match="issuer"):
        exchange(provider)
    google.id_token = google.mint(iss="accounts.google.com", aud=[CLIENT_ID, "x"])
    assert exchange(provider).sub == "g-1"


def test_google_provider_rejects_bad_signature_and_alg():
    google = FakeGoogle()
    provider = make_provider(google)
    good = google.mint()
    head, body, sig = good.split(".")
    forged_body = b64u(json.dumps({"sub": "admin", "aud": CLIENT_ID}).encode())
    google.id_token = f"{head}.{forged_body}.{sig}"
    with pytest.raises(SignInError, match="signature"):
        exchange(provider)

    none_head = b64u(json.dumps({"alg": "none", "kid": "kid-1"}).encode())
    google.id_token = f"{none_head}.{body}."
    with pytest.raises(SignInError):
        exchange(provider)

    google.id_token = "not-a-jwt"
    with pytest.raises(SignInError):
        exchange(provider)


def test_google_provider_refreshes_jwks_once_on_unknown_kid():
    google = FakeGoogle()
    provider = make_provider(google)
    google.id_token = google.mint()
    exchange(provider)
    google.kid = "kid-rotated"  # Google rotated its key; the cache is stale
    google.id_token = google.mint()
    assert exchange(provider).sub == "g-1"
    assert google.requests.count("/oauth2/v3/certs") == 2


def test_unknown_kid_refreshes_jwks_once_without_rediscovery():
    google = FakeGoogle()
    provider = make_provider(google)
    google.id_token = google.mint()
    google.kid = "kid-never-published"
    google.keys_override = [dict(google.jwk("kid-other"))]
    google.id_token = google.mint()
    with pytest.raises(SignInError):
        exchange(provider)
    assert google.requests.count("/oauth2/v3/certs") == 1
    assert google.requests.count("/.well-known/openid-configuration") == 1


@pytest.mark.parametrize(
    "bad", [{"e": 5, "n": 5}, {"e": None, "n": "AQAB"}, {"e": "!!", "n": "x"}]
)
def test_malformed_jwk_is_a_sign_in_error(bad):
    google = FakeGoogle()
    provider = make_provider(google)
    google.keys_override = [{"kid": google.kid, **bad}]
    google.id_token = google.mint()
    with pytest.raises(SignInError):
        exchange(provider)


def test_authorize_url_carries_pkce_state_and_nonce():
    provider = make_provider(FakeGoogle())
    url = provider.authorize_url(state="st", nonce="no", code_challenge="ch")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for part in (
        "state=st",
        "nonce=no",
        "code_challenge=ch",
        "code_challenge_method=S256",
    ):
        assert part in url
    assert re.search(
        r"redirect_uri=https%3A%2F%2Fcloud.example.com%2Fauth%2Fgoogle%2Fcallback", url
    )


def test_create_app_builds_google_provider_when_configured(settings, clock):
    from dataclasses import replace

    from ice_cloud.app import create_app

    configured = replace(
        settings,
        google_client_id="id",
        google_client_secret="secret",
        public_url="https://cloud.example.com/",
    )
    app = create_app(configured, now=clock)
    assert isinstance(app.state.provider, auth.GoogleProvider)
    assert create_app(settings, now=clock).state.provider is None


def test_session_timedelta_constants():
    assert IDLE_LIMIT == timedelta(days=30) and ABSOLUTE_LIMIT == timedelta(days=90)


def test_create_app_closes_its_http_client_on_shutdown(settings, clock, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ice_cloud.app import create_app

    closed: list[bool] = []
    real = httpx.AsyncClient.aclose

    async def spy(self):
        closed.append(True)
        await real(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy)
    configured = replace(
        settings,
        google_client_id="id",
        google_client_secret="secret",
        public_url="https://cloud.example.com",
    )
    with TestClient(create_app(configured, now=clock)):
        assert closed == []
    assert closed == [True]


def test_machine_role_member_admin_and_stranger(cloud, signer):
    from tests.cloud.conftest import push
    from tests.cloud.reports import make_report

    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    repo = cloud.app.state.repo
    settings = cloud.app.state.settings
    repo.claim_machine(signer.fingerprint, owner.id, cloud.app.state.now())
    tech = login(cloud, "tech@example.com")
    repo.grant_tech(tech.id, signer.fingerprint, owner.id, cloud.app.state.now())
    admin = login(cloud, "admin@example.com")
    other = login(cloud, "other@example.com")
    fp = signer.fingerprint
    assert auth.machine_role(settings, repo, owner, fp) == "owner"
    assert auth.machine_role(settings, repo, tech, fp) == "tech"
    assert auth.machine_role(settings, repo, admin, fp) == "admin"
    assert auth.machine_role(settings, repo, other, fp) is None
