"""`/healthz`: the unauthenticated liveness probe the image HEALTHCHECK calls."""

from fastapi.testclient import TestClient
from ice_edge.access.access import AccessStore
from ice_edge.web import routes
from ice_edge.web.server import app


def test_healthz_ok_without_a_cookie_and_sets_none(tmp_path) -> None:
    store = AccessStore(path=tmp_path / "access.json")
    routes.set_access_store(store)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")
    assert "set-cookie" not in response.headers


def test_healthz_is_not_redirected_in_setup_mode(tmp_path) -> None:
    store = AccessStore(path=tmp_path / "access.json")
    assert store.setup_mode
    routes.set_access_store(store)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
    assert "set-cookie" not in response.headers


def test_healthz_works_with_no_access_store_wired() -> None:
    routes.set_access_store(None)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/healthz").status_code == 200
