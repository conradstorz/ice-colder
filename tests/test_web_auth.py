"""Unit tests for web_interface/auth.py: cookies, client keying, require()."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services.access import AccessStore, Permission, Role
from web_interface import auth


@pytest.fixture
def store(tmp_path):
    s = AccessStore(path=tmp_path / "access.json")
    auth.set_access_store(s)
    auth.backoff.set_trusted_proxies([])
    yield s
    auth.set_access_store(None)


@pytest.fixture
def app(store):
    api = FastAPI()

    @api.get("/open")
    def open_page(principal=Depends(auth.require(Permission.view_status))):
        return {"user": principal.user.name}

    @api.get("/secret")
    def secret_page(principal=Depends(auth.require(Permission.edit_secrets))):
        return {"ok": True}

    return api


def _login(store, client, role=Role.owner, shared=False):
    user = store.create_user("Ada", None, role, "1379")
    device, token = store.create_device("Test device", shared=shared)
    store.trust_device(device.id, user.id)
    session = store.create_session(user.id, device.id)
    client.cookies.set(auth.DEVICE_COOKIE, token)
    client.cookies.set(auth.SESSION_COOKIE, session)
    return user, device


class TestRequire:
    def test_page_request_without_a_session_redirects_to_login(self, app):
        with TestClient(app, follow_redirects=False) as c:
            resp = c.get("/open")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_htmx_request_without_a_session_gets_hx_redirect(self, app):
        with TestClient(app) as c:
            resp = c.get("/open", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

    def test_permitted_role_passes(self, app, store):
        with TestClient(app) as c:
            _login(store, c, Role.owner)
            resp = c.get("/open")
        assert resp.status_code == 200
        assert resp.json()["user"] == "Ada"

    def test_missing_permission_is_403(self, app, store):
        with TestClient(app) as c:
            _login(store, c, Role.loader)
            resp = c.get("/secret")
        assert resp.status_code == 403

    def test_expired_session_redirects_not_403(self, app, store):
        with TestClient(app, follow_redirects=False) as c:
            _login(store, c, Role.owner)
            store.end_all_sessions()
            resp = c.get("/open")
        assert resp.status_code == 303


class TestClientKeying:
    def test_resolved_device_cookie_keys_on_the_device_id(self, store):
        device, token = store.create_device("Tablet", shared=True)
        request = _fake_request(cookies={auth.DEVICE_COOKIE: token})
        assert auth.client_key(request) == device.id

    def test_unresolvable_cookie_falls_back_to_the_ip(self, store):
        request = _fake_request(cookies={auth.DEVICE_COOKIE: "bogus"}, peer="10.1.2.3")
        assert auth.client_key(request) == "10.1.2.3"

    def test_no_cookie_falls_back_to_the_ip(self, store):
        assert auth.client_key(_fake_request(peer="10.1.2.3")) == "10.1.2.3"

    def test_is_trusted_client(self, store):
        user = store.create_user("Ada", None, Role.owner, "1379")
        device, token = store.create_device("Tablet", shared=True)
        request = _fake_request(cookies={auth.DEVICE_COOKIE: token})
        assert auth.is_trusted_client(request, user.id) is False
        store.trust_device(device.id, user.id)
        assert auth.is_trusted_client(request, user.id) is True


class _FakeUrl:
    def __init__(self, scheme):
        self.scheme = scheme


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, cookies, peer, scheme):
        self.cookies = cookies
        self.client = _FakeClient(peer)
        self.headers = {}
        self.url = _FakeUrl(scheme)


def _fake_request(cookies=None, peer="127.0.0.1", scheme="http"):
    return _FakeRequest(cookies or {}, peer, scheme)


class TestCookieFlags:
    def test_http_request_gets_no_secure_flag(self, app, store):
        api = app

        @api.get("/setcookie")
        def setcookie(request: __import__("fastapi").Request):
            from fastapi.responses import JSONResponse

            resp = JSONResponse({})
            auth.set_cookie(
                resp,
                request,
                auth.SESSION_COOKIE,
                "abc",
                max_age=None,
                backoff=auth.backoff,
            )
            return resp

        with TestClient(api) as c:
            resp = c.get("/setcookie")
        header = resp.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")
        assert "Secure" not in header
