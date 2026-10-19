import base64

import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
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


@pytest.fixture
def cloud_app(settings, clock):
    return create_app(settings, now=clock)


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


def select(cloud: TestClient, signer: Signer) -> None:
    fingerprint = signer.fingerprint
    r = cloud.post(f"/fleet/select/{fingerprint}")
    assert r.status_code == 303 and r.headers["location"] == "/"
