"""The signed-in identity chip, the You page and the banner's common name."""

from tests.cloud.conftest import claim, login, own_and_select, push, select
from tests.cloud.reports import make_report


def _bar(html: str) -> str:
    return html.split('id="bar"', 1)[1].split("</header>", 1)[0]


def _named_report(**kw):
    return make_report(contract="0.4.0", common_name="Lobby Ice", **kw)


def test_owner_sees_chip_and_me_page(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    bar = _bar(cloud.get("/").text)
    assert 'href="/me"' in bar and "owner" in bar and "Test User" in bar
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


def test_admin_non_member_and_admin_owner(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, _named_report())
    claim(cloud, signer, owner)
    login(cloud, "admin@example.com")
    select(cloud, signer)
    assert "<strong>admin</strong> on Lobby Ice" in cloud.get("/me").text


def test_admin_who_owns_is_owner(cloud, signer):
    own_and_select(cloud, signer, _named_report(), email="admin@example.com")
    assert "<strong>owner</strong> on Lobby Ice" in cloud.get("/me").text


def test_fleet_bar_has_no_role(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    bar = _bar(cloud.get("/fleet").text)
    assert 'href="/me"' in bar and "Test User" in bar
    assert "rounded-full bg-slate-600" not in bar


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


def test_banner_shows_common_name_before_machine_id(cloud, signer):
    own_and_select(cloud, signer, _named_report())
    text = cloud.get("/health").text
    assert text.index("Lobby Ice") < text.index("m-0042")
