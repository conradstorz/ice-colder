"""The signed-in identity chip, the You page and the banner's common name."""

from tests.cloud.conftest import (
    claim,
    login,
    marked_bundle,
    own_and_select,
    push,
    register,
    select,
    short,
)
from tests.cloud.reports import make_report


def _bar(html: str) -> str:
    return html.split('id="bar"', 1)[1].split("</header>", 1)[0]


def _named_report(**kw):
    return make_report(contract="0.4.0", common_name="Lobby Ice", **kw)


def test_owner_sees_chip_and_me_page(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    bar = _bar(cloud.get("/").text)
    assert 'href="/me"' in bar and ">owner</span>" in bar and "Test User" in bar
    r = cloud.get("/me")
    assert r.status_code == 200
    assert "<strong>owner</strong> on Lobby Ice (m-0042)" in r.text
    assert "Sign out" in r.text
    assert "Last signed in" not in r.text
    assert "view_status" not in r.text  # labels, not identifiers


def test_me_lists_permission_labels(cloud, signer):
    from ice_contracts.access import PERMISSION_LABELS, Permission

    own_and_select(cloud, signer, _named_report())
    text = cloud.get("/me").text
    assert PERMISSION_LABELS[Permission.view_status] in text


def test_tech_role(cloud, signer):
    owner = own_and_select(cloud, signer, _named_report())
    tech = login(cloud, "tech@example.com")
    cloud.app.state.repo.grant_tech(
        tech.id, signer.fingerprint, owner.id, cloud.app.state.now()
    )
    select(cloud, signer)
    assert "<strong>tech</strong> on Lobby Ice" in cloud.get("/me").text


def test_admin_non_member_cannot_select(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, _named_report())
    claim(cloud, signer, owner)
    login(cloud, "admin@example.com")
    r = cloud.post(f"/fleet/select/{short(signer)}", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert "No machine selected" in cloud.get("/me").text


def test_admin_who_owns_is_owner(cloud, signer):
    own_and_select(cloud, signer, _named_report(), email="admin@example.com")
    assert "<strong>owner</strong> on Lobby Ice" in cloud.get("/me").text


def test_fleet_bar_has_no_role(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    bar = _bar(cloud.get("/fleet").text)
    assert 'href="/me"' in bar and "Test User" in bar
    assert "rounded-full bg-slate-600" not in bar
    for role in ("owner", "tech", "admin"):
        assert f">{role}</span>" not in bar


def test_me_without_selection(cloud):
    login(cloud, "owner@example.com")
    r = cloud.get("/me")
    assert r.status_code == 200
    assert "No machine selected" in r.text and 'href="/fleet"' in r.text


def test_me_old_report_has_no_common_name(cloud, signer):
    own_and_select(cloud, signer, make_report())
    assert "<strong>owner</strong> on m-0042" in cloud.get("/me").text


def test_anonymous_me_redirects(cloud):
    r = cloud.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_badge_shows_common_name_instead_of_machine_id(cloud, signer):
    import re

    own_and_select(cloud, signer, _named_report())
    text = cloud.get("/health").text
    m = re.search(r'id="context-badge"[^>]*>(?:<span[^>]*>)?([^<]*)<', text)
    assert m and m.group(1).strip() == "Lobby Ice"


def test_no_report_page_bar_carries_role(cloud, signer):
    owner = login(cloud, "owner@example.com")
    register(cloud, signer, "m-quiet")
    claim(cloud, signer, owner)
    select(cloud, signer)
    r = cloud.get("/")
    assert "No report from this machine yet" in r.text
    assert ">owner</span>" in _bar(r.text)


def test_me_renders_from_installed_ui_with_trusted_old_bundle(
    trusting_cloud, tmp_path, signer
):
    from tests.cloud.test_portal import put_bundle

    digest, data = marked_bundle(tmp_path, "OLD-BUNDLE", drop=("me.html",))
    cloud = trusting_cloud(digest)
    own_and_select(cloud, signer, make_report(digest=digest))
    assert put_bundle(cloud, signer, digest, data).status_code == 201
    assert cloud.get("/health/faults").status_code == 200  # bundle is in effect
    r = cloud.get("/me")
    assert r.status_code == 200
    assert "<strong>owner</strong> on m-0042" in r.text


def test_cloud_pages_are_marked_remote(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    for path in ("/", "/fleet", "/me"):
        html = cloud.get(path).text
        assert ">REMOTE</span>" in _bar(html), path
        assert "LOCAL" not in html, path
        assert "bg-indigo-900" in _bar(html) and "bg-sky-100" in html, path
        assert '<pattern id="step"' in html, path


def test_cloud_login_page_is_marked_remote(cloud):
    html = cloud.get("/login").text
    assert 'id="source-tag"' in html and ">REMOTE</span>" in html
    assert "context-badge" not in html
