"""The edge fills the bar badge: machine name, deep link to the cloud portal."""

from types import SimpleNamespace

from ice_common.config_model import ConfigModel

from ice_edge.web import context
from ice_edge.web import routes


def _cfg(
    common_name="", machine_id="vm-7", cloud_enabled=False, url="http://cloud.test/"
):
    cfg = ConfigModel()
    cfg.machine_id = machine_id
    cfg.physical.common_name = common_name
    cfg.cloud.enabled = cloud_enabled
    cfg.cloud.url = url
    return cfg


def _identity(short="aaaa-bbbb-cccc-dddd"):
    return SimpleNamespace(short_fingerprint=short)


def test_badge_label_prefers_common_name():
    routes.set_config_object(_cfg(common_name="Lobby Ice"))
    assert context.context_badge().label == "Lobby Ice"


def test_badge_falls_back_to_machine_id():
    routes.set_config_object(_cfg(common_name=""))
    assert context.context_badge().label == "vm-7"


def test_badge_links_to_cloud_when_enabled_and_identity():
    routes.set_config_object(_cfg(cloud_enabled=True))
    context.set_machine_identity(_identity())
    badge = context.context_badge()
    assert badge.url == "http://cloud.test/m/aaaa-bbbb-cccc-dddd"
    assert badge.external is True
    assert badge.tone == "local"
    assert badge.title is None


def test_badge_not_clickable_when_cloud_disabled():
    routes.set_config_object(_cfg(cloud_enabled=False))
    context.set_machine_identity(_identity())
    assert context.context_badge().url is None


def test_badge_not_clickable_without_identity():
    routes.set_config_object(_cfg(cloud_enabled=True))
    context.set_machine_identity(None)
    assert context.context_badge().url is None


def test_template_context_has_no_badge_without_config():
    context.config = None
    ctx = context.template_context(SimpleNamespace(cookies={}, headers={}))
    assert "badge" not in ctx


def test_home_page_renders_badge(wired, client):
    cfg, _vmc, _inv, _store = wired
    cfg.physical.common_name = "Lobby Ice"
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="context-badge"' in response.text
    assert "Lobby Ice" in response.text


def test_login_page_has_no_badge(wired, anonymous):
    response = anonymous.get("/login")
    assert response.status_code == 200
    assert "context-badge" not in response.text
