import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from ice_cloud.settings import CloudSettings
from tests.cloud.reports import FP, NOW, later


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


def push(cloud: TestClient, report, fingerprint: str = FP) -> None:
    r = cloud.post(
        f"/api/v1/machines/{fingerprint}/report",
        content=report.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 204


def select(cloud: TestClient, fingerprint: str = FP) -> None:
    r = cloud.post(f"/fleet/select/{fingerprint}")
    assert r.status_code == 303 and r.headers["location"] == "/"
