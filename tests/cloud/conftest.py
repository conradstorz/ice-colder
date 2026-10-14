import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
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


@pytest.fixture
def cloud_app(tmp_path, clock):
    return create_app(data_dir=tmp_path / "cloud_data", now=clock)


@pytest.fixture
def cloud(cloud_app):
    with TestClient(cloud_app, follow_redirects=False) as c:
        yield c


def push(cloud: TestClient, report) -> None:
    r = cloud.post(
        f"/api/v1/machines/{report.machine_id}/report",
        content=report.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 204


def select(cloud: TestClient, machine_id: str) -> None:
    r = cloud.post(f"/fleet/select/{machine_id}")
    assert r.status_code == 303 and r.headers["location"] == "/"
