import base64

import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from ice_cloud.auth import IdentityClaims, SignInError
from ice_cloud.store.repository import Account
from ice_cloud.settings import CloudSettings
from ice_contracts.cloud import signing
from ice_contracts.cloud.identity import RegisterMachine
from tests.cloud.reports import NOW, later


class Clock:
    """The portal's injected clock; tests move it instead of waiting."""

    def __init__(self) -> None:
        self.value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = later(seconds)


@pytest.fixture
def clock():
    return Clock()


def make_settings(data_dir) -> CloudSettings:
    return CloudSettings(
        data_dir=data_dir,
        public_url=None,
        google_client_id=None,
        google_client_secret=None,
        admins=frozenset({"admin@example.com"}),
        session_key=b"k" * 32,
        insecure_cookies=True,
    )


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path / "cloud_data")


class FakeProvider:
    """An OIDC provider with fixed, mutable claims; no network, no Google."""

    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims
        self.exchanges: list[dict[str, str]] = []

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return f"/auth/google/callback?code=fake&state={state}"

    async def exchange(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> IdentityClaims:
        self.exchanges.append(
            {"code": code, "code_verifier": code_verifier, "nonce": nonce}
        )
        if not self.claims.email_verified:
            raise SignInError("Your Google email address is not verified")
        return self.claims


@pytest.fixture
def provider():
    return FakeProvider(
        IdentityClaims(
            sub="sub-default", email="user@example.com", email_verified=True, name="U"
        )
    )


@pytest.fixture
def cloud_app(settings, clock, provider):
    return create_app(settings, now=clock, provider=provider)


def login(
    cloud: TestClient, email: str, sub: str | None = None, name: str = "Test User"
) -> Account:
    """Sign the client in as ``email`` by driving start then callback."""
    cloud.app.state.provider.claims = IdentityClaims(
        sub=sub or f"sub-{email}", email=email, email_verified=True, name=name
    )
    start = cloud.get("/auth/google/start")
    assert start.status_code == 303
    callback = cloud.get(start.headers["location"])
    assert callback.status_code == 303, callback.text
    account = cloud.app.state.repo.account_by_email(email)
    assert account is not None
    return account


@pytest.fixture
def cloud(cloud_app):
    with TestClient(cloud_app, follow_redirects=False) as c:
        yield c


class Signer:
    """A machine identity for tests: a fresh Ed25519 key signing requests."""

    def __init__(self) -> None:
        self._private = signing.generate_private_key()
        self.public_key = signing.public_key_from_private(self._private)
        self.public_key_b64 = base64.b64encode(self.public_key).decode("ascii")
        self.fingerprint = signing.fingerprint(self.public_key)

    def headers(
        self, method: str, path: str, body: bytes = b"", *, now=NOW
    ) -> dict[str, str]:
        ts = signing.format_timestamp(now)
        message = signing.canonical_message(method, path, ts, body)
        return {
            signing.HEADER_MACHINE: self.fingerprint,
            signing.HEADER_TIMESTAMP: ts,
            signing.HEADER_SIGNATURE: signing.sign(self._private, message),
        }


@pytest.fixture
def signer():
    return Signer()


def signed(
    cloud: TestClient, signer: Signer, method: str, path: str, body: bytes = b""
):
    """Send one correctly signed request, stamped with the cloud's own clock."""
    headers = signer.headers(method, path, body, now=cloud.app.state.now())
    if body:
        headers["content-type"] = "application/json"
    return cloud.request(method, path, content=body, headers=headers)


def register(cloud: TestClient, signer: Signer, label: str = "m-0042") -> str:
    body = RegisterMachine(public_key_b64=signer.public_key_b64, label=label)
    r = signed(
        cloud,
        signer,
        "POST",
        "/api/v1/machines/register",
        body.model_dump_json().encode(),
    )
    assert r.status_code == 200
    return r.json()["fingerprint"]


def push(cloud: TestClient, signer: Signer, report) -> None:
    """Register (idempotent) and push one report as the signer's machine."""
    register(cloud, signer, report.machine_id)
    r = signed(
        cloud,
        signer,
        "POST",
        f"/api/v1/machines/{signer.fingerprint}/report",
        report.model_dump_json().encode(),
    )
    assert r.status_code == 204


def short(signer: Signer) -> str:
    return signing.short_fingerprint(signer.fingerprint)


def claim(cloud: TestClient, signer: Signer, account: Account) -> None:
    """Bind the signer's (registered) machine to ``account`` through the repo."""
    assert cloud.app.state.repo.claim_machine(
        signer.fingerprint, account.id, cloud.app.state.now()
    )


def select(cloud: TestClient, signer: Signer) -> None:
    r = cloud.post(f"/fleet/select/{short(signer)}", headers={"HX-Request": "true"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def own_and_select(
    cloud: TestClient, signer: Signer, report, email: str = "owner@example.com"
) -> Account:
    """Sign in as ``email``, push ``report``, claim the machine, select it."""
    account = login(cloud, email)
    push(cloud, signer, report)
    claim(cloud, signer, account)
    select(cloud, signer)
    return account
