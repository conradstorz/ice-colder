import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from tests.cloud.conftest import make_settings
from ice_ui.bundle import build_bundle, ui_digest
from tests.cloud.reports import make_report


@pytest.fixture
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        yield c


def _post(client, report, machine_id=None):
    return client.post(
        f"/api/v1/machines/{machine_id or report.machine_id}/report",
        content=report.model_dump_json(),
        headers={"content-type": "application/json"},
    )


def test_report_is_accepted_and_stored(client, tmp_path):
    assert _post(client, make_report()).status_code == 204
    assert (tmp_path / "machines" / "m-0042.json").is_file()


def test_machine_id_mismatch_is_422(client, tmp_path):
    assert _post(client, make_report(machine_id="m-0042"), "m-0099").status_code == 422
    assert not (tmp_path / "machines").exists()


def test_invalid_body_is_422(client):
    r = client.post("/api/v1/machines/m-1/report", json={"machine_id": "m-1"})
    assert r.status_code == 422


def test_bundle_put_201_then_200_then_400_for_corrupt_bytes(client):
    data = build_bundle()
    url = f"/api/v1/ui-bundles/{ui_digest()}"
    assert client.put(url, content=data).status_code == 201
    assert client.put(url, content=data).status_code == 200
    bad = client.put(f"/api/v1/ui-bundles/{'a' * 64}", content=b"corrupt")
    assert bad.status_code == 400
    assert "detail" in bad.json()


def test_bundle_with_wrong_digest_in_url_is_400(client):
    r = client.put(f"/api/v1/ui-bundles/{'b' * 64}", content=build_bundle())
    assert r.status_code == 400
    assert "digest" in r.json()["detail"]


def test_oversize_bundle_is_rejected_before_extraction(client, tmp_path):
    big = b"\0" * (20 * 1024 * 1024 + 1)
    r = client.put(f"/api/v1/ui-bundles/{'d' * 64}", content=big)
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]
    assert not (tmp_path / "ui").exists()


def test_oversize_bundle_without_content_length_is_cut_off(client):
    def body():
        for _ in range(21):
            yield b"\0" * (1024 * 1024)

    r = client.put(f"/api/v1/ui-bundles/{'d' * 64}", content=body())
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]
