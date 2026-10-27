"""One identity, two hats: the hat is the URL (spec 2026-10-24-cloud-admin-hat)."""

import re

from tests.cloud.conftest import login, own_and_select, short
from tests.cloud.reports import make_report


def _bar(html: str) -> str:
    return html.split('id="bar"', 1)[1].split("</header>", 1)[0]


_HAT_RE = re.compile(r'<a id="hat-switch" href="([^"]*)"[^>]*>([^<]*)</a>')


def _hat(html: str) -> tuple[str, str] | None:
    """The hat-switch anchor as (href, label), or None when the page has none."""
    m = _HAT_RE.search(html)
    return (m.group(1), m.group(2)) if m else None


ADMIN_HAT = ("/admin", "Admin")
MY_MACHINES_HAT = ("/fleet", "My machines")


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
        assert _hat(bar) == MY_MACHINES_HAT, path  # breadcrumbs may say Admin


def test_portal_pages_offer_the_admin_hat_to_an_admin(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    for path in ("/", "/health", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert _hat(bar) == ADMIN_HAT, path
        assert ">admin</span>" not in bar, path
    assert ">owner</span>" in _bar(cloud.get("/").text)


def test_portal_pages_show_no_switch_to_an_owner(cloud, signer):
    own_and_select(cloud, signer, make_report())
    for path in ("/", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert _hat(bar) is None, path


def test_people_and_alerts_pages_offer_the_switch_to_an_admin_owner(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    for path in (
        f"/machines/{short(signer)}/people",
        f"/machines/{short(signer)}/alerts",
    ):
        bar = _bar(cloud.get(path).text)
        assert _hat(bar) == ADMIN_HAT, path


def test_login_page_has_no_switch(cloud):
    text = cloud.get("/login").text
    assert _hat(text) is None


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
