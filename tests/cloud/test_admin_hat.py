"""One identity, two hats: the hat is the URL (spec 2026-10-24-cloud-admin-hat)."""

from tests.cloud.conftest import login, own_and_select, short
from tests.cloud.reports import make_report


def _bar(html: str) -> str:
    return html.split('id="bar"', 1)[1].split("</header>", 1)[0]


ADMIN_LINK = 'href="/admin"'
MY_MACHINES_LINK = 'href="/fleet"'


def test_admin_pages_wear_the_admin_hat(cloud):
    login(cloud, "admin@example.com")
    for path in (
        "/admin",
        "/admin/machines",
        "/admin/owners",
        "/admin/accounts",
        "/admin/alerts",
    ):
        bar = _bar(cloud.get(path).text)
        assert ">admin</span>" in bar, path
        assert ">My machines</a>" in bar and MY_MACHINES_LINK in bar, path
        assert 'shrink-0">Admin</a>' not in bar, path  # breadcrumbs may say Admin


def test_portal_pages_offer_the_admin_hat_to_an_admin(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    for path in ("/", "/health", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" in bar and ADMIN_LINK in bar, path
        assert ">admin</span>" not in bar, path
    assert ">owner</span>" in _bar(cloud.get("/").text)


def test_portal_pages_show_no_switch_to_an_owner(cloud, signer):
    own_and_select(cloud, signer, make_report())
    for path in ("/", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" not in bar and ">My machines</a>" not in bar, path


def test_people_and_alerts_pages_offer_the_switch_to_an_admin_owner(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    for path in (
        f"/machines/{short(signer)}/people",
        f"/machines/{short(signer)}/alerts",
    ):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" in bar, path


def test_login_page_has_no_switch(cloud):
    text = cloud.get("/login").text
    assert ">Admin</a>" not in text and ">My machines</a>" not in text


def test_me_names_the_platform_admin(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    text = cloud.get("/me").text
    assert "Platform admin" in text and 'href="/admin"' in text
    assert "<strong>owner</strong> on" in text


def test_me_without_selection_still_names_the_platform_admin(cloud):
    login(cloud, "admin@example.com")
    text = cloud.get("/me").text
    assert "Platform admin" in text and "No machine selected" in text


def test_me_says_nothing_about_admin_to_an_owner(cloud, signer):
    own_and_select(cloud, signer, make_report())
    assert "Platform admin" not in cloud.get("/me").text
