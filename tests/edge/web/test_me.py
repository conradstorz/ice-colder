"""The signed-in identity chip in the bar and the /me ("You") level."""

import pytest
from fastapi.testclient import TestClient
from ice_common.config_model import ConfigModel
from ice_contracts.access import PERMISSION_LABELS
from ice_edge.access.access import ROLE_PERMISSIONS, AccessStore, Permission, Role
from ice_edge.customer_ui.display_controller import DisplayController
from ice_edge.web import auth as web_auth
from ice_edge.web import context, routes
from ice_edge.web.server import app
from tests.dom_utils import bar

NAMES = {
    Role.owner: None,
    Role.tech: "Tess Tech",
    Role.secretary: "Sam",
    Role.loader: "Lou",
}


def _login(login_as, role):
    client = login_as(role, name=NAMES[role]) if role != Role.owner else login_as(role)
    return client


@pytest.mark.parametrize("role", list(Role))
def test_bar_shows_identity_chip(login_as, role):
    client = _login(login_as, role)
    resp = client.get("/")
    assert resp.status_code == 200
    top = bar(resp.text)
    assert 'href="/me"' in top
    assert role.value in top
    name = NAMES[role] or context.access_store.owner().name
    assert name in top


@pytest.mark.parametrize("role", list(Role))
def test_me_page(login_as, role):
    client = _login(login_as, role)
    resp = client.get("/me")
    assert resp.status_code == 200
    text = resp.text
    name = NAMES[role] or context.access_store.owner().name
    assert name in text
    cfg = context.config
    assert (
        f"<strong>{role.value}</strong> on "
        f"{cfg.physical.common_name} ({cfg.machine_id})"
    ) in text
    allowed = ROLE_PERMISSIONS[role]
    for perm in Permission:
        if perm in allowed:
            assert PERMISSION_LABELS[perm] in text
        else:
            assert PERMISSION_LABELS[perm] not in text
    assert 'hx-post="/logout"' in text
    assert "Sign out" in text


def test_me_anonymous_redirects_to_login(anonymous):
    resp = anonymous.get("/me")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_has_no_me_link(anonymous):
    resp = anonymous.get("/login")
    assert 'href="/me"' not in resp.text


def test_setup_page_has_no_me_link(tmp_path):
    web_auth.backoff.reset()
    routes.set_config_object(ConfigModel())
    routes.set_access_store(AccessStore(path=tmp_path / "access.json"))
    routes.set_display_controller(DisplayController())
    c = TestClient(app, follow_redirects=False)
    try:
        resp = c.get("/setup")
        assert resp.status_code == 200
        assert 'href="/me"' not in resp.text
    finally:
        c.close()
        web_auth.backoff.reset()
        web_auth.backoff.set_trusted_proxies([])
