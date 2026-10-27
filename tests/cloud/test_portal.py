import gzip
import io
import json
import re
import shutil
import tarfile

import pytest

from ice_ui.bundle import INSTALLED_ROOT, build_bundle
from tests.cloud.conftest import (
    marked_bundle,
    Signer,
    claim,
    login,
    own_and_select,
    push,
    register,
    select,
    short,
    signed,
)
from tests.cloud.reports import make_report


def put_bundle(cloud, signer, digest, data):
    register(cloud, signer)
    return signed(cloud, signer, "PUT", f"/api/v1/ui-bundles/{digest}", data)


PAGES = ["/", "/status", "/pill", "/kpi", "/health", "/health/faults"]


def test_anonymous_machine_pages_redirect_to_login(cloud):
    for path in PAGES + ["/fleet", "/controls"]:
        r = cloud.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login"), path


def test_signed_in_without_selection_redirects_every_machine_page_to_fleet(cloud):
    login(cloud, "owner@example.com")
    for path in PAGES:
        r = cloud.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/fleet"


def test_fleet_lists_only_my_machines_with_roles(cloud):
    a, b, c = Signer(), Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    other = login(cloud, "other@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    push(cloud, b, make_report(machine_id="m-B"))
    push(cloud, c, make_report(machine_id="m-C"))
    claim(cloud, a, owner)
    claim(cloud, b, other)
    cloud.app.state.repo.grant_tech(
        owner.id, b.fingerprint, other.id, cloud.app.state.now()
    )
    login(cloud, "owner@example.com")
    page = cloud.get("/fleet")
    assert page.status_code == 200
    assert "m-A" in page.text and "m-B" in page.text and "m-C" not in page.text
    assert short(a) in page.text and short(c) not in page.text
    assert f"/fleet/select/{short(a)}" in page.text
    assert f"/machines/{short(a)}/people" in page.text
    assert f"/machines/{short(b)}/people" not in page.text
    assert ">owner<" in page.text and ">tech<" in page.text
    assert "Online" in page.text and "1 active fault" in page.text
    assert 'href="/claim"' in page.text


def test_fleet_empty_state_still_offers_claim(cloud):
    login(cloud, "owner@example.com")
    page = cloud.get("/fleet")
    assert "You have no machines yet" in page.text and 'href="/claim"' in page.text


def test_fleet_shows_never_reported_machine(cloud, signer):
    owner = login(cloud, "owner@example.com")
    register(cloud, signer, "m-quiet")
    claim(cloud, signer, owner)
    assert "never reported" in cloud.get("/fleet").text


def test_admin_fleet_lists_memberships_only(cloud):
    a, b = Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    push(cloud, b, make_report(machine_id="m-B"))
    claim(cloud, a, owner)
    login(cloud, "admin@example.com")
    page = cloud.get("/fleet")
    assert "m-A" not in page.text and "m-B" not in page.text
    assert ">admin<" not in page.text and "Fleet dashboard" not in page.text
    assert "You have no machines yet" in page.text
    assert (
        cloud.post(
            f"/fleet/select/{short(b)}", headers={"HX-Request": "true"}
        ).status_code
        == 404
    )
    assert cloud.get(f"/m/{short(b)}").status_code == 404


def test_admin_who_owns_is_listed_as_owner(cloud):
    a = Signer()
    admin = login(cloud, "admin@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    claim(cloud, a, admin)
    page = cloud.get("/fleet")
    assert "m-A" in page.text and ">owner<" in page.text


def test_select_sets_a_non_credential_cookie(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    r = cloud.post(f"/fleet/select/{short(signer)}", headers={"HX-Request": "true"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert f"ice_cloud_machine={signer.fingerprint}" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_select_non_member_404(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    login(cloud, "stranger@example.com")
    assert (
        cloud.post(
            f"/fleet/select/{short(signer)}", headers={"HX-Request": "true"}
        ).status_code
        == 404
    )
    assert cloud.get(f"/m/{short(signer)}").status_code == 404
    assert (
        cloud.post("/fleet/select/m-ghost", headers={"HX-Request": "true"}).status_code
        == 404
    )
    assert (
        cloud.post("/fleet/select/bad%20id", headers={"HX-Request": "true"}).status_code
        == 404
    )


def test_machine_pages_require_access_and_redirect_to_fleet(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    login(cloud, "stranger@example.com")
    cloud.cookies.set("ice_cloud_machine", signer.fingerprint)
    for path in PAGES:
        r = cloud.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/fleet"


def test_fragment_without_access_is_204_for_htmx(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    login(cloud, "stranger@example.com")
    cloud.cookies.set("ice_cloud_machine", signer.fingerprint)
    for path in ("/status", "/pill", "/kpi"):
        assert cloud.get(path, headers={"HX-Request": "true"}).status_code == 204
        assert cloud.get(path).status_code == 303


def test_admin_stale_selection_cookie_redirects_to_fleet(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    login(cloud, "admin@example.com")
    cloud.cookies.set("ice_cloud_machine", signer.fingerprint)
    for path in PAGES:
        r = cloud.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/fleet"
    assert cloud.get("/static/app.css").status_code == 200


def test_admin_stale_selection_fragment_is_204_for_htmx(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    login(cloud, "admin@example.com")
    cloud.cookies.set("ice_cloud_machine", signer.fingerprint)
    for path in ("/status", "/pill", "/kpi"):
        assert cloud.get(path, headers={"HX-Request": "true"}).status_code == 204
        assert cloud.get(path).status_code == 303
    assert cloud.get("/static/app.css").status_code == 200


def test_cookie_alone_is_not_enough(cloud, signer):
    tech = login(cloud, "tech@example.com")
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    repo = cloud.app.state.repo
    repo.grant_tech(tech.id, signer.fingerprint, owner.id, cloud.app.state.now())
    login(cloud, "tech@example.com")
    select(cloud, signer)
    assert cloud.get("/health/faults").status_code == 200
    repo.revoke(tech.id, signer.fingerprint)
    r = cloud.get("/health/faults")
    assert r.status_code == 303 and r.headers["location"] == "/fleet"


def test_deep_link_selects(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    r = cloud.get(f"/m/{short(signer)}")
    # Never lands on a machine page after a cross-site click: fleet list only.
    assert r.status_code == 303 and r.headers["location"] == "/fleet"
    assert f"ice_cloud_machine={signer.fingerprint}" in r.headers["set-cookie"]
    assert cloud.get("/health/faults").status_code == 200


def _fleet_order(cloud, *labels):
    text = cloud.get("/fleet").text
    return sorted(labels, key=text.index)


def test_fleet_orders_most_recent_first(cloud):
    a, b = Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    push(cloud, b, make_report(machine_id="m-B"))
    claim(cloud, a, owner)
    claim(cloud, b, owner)
    assert _fleet_order(cloud, "m-B", "m-A") == ["m-A", "m-B"]
    r = cloud.post(f"/fleet/select/{short(b)}", headers={"HX-Request": "true"})
    assert r.status_code == 303
    assert _fleet_order(cloud, "m-A", "m-B") == ["m-B", "m-A"]


def test_deep_link_records_view(cloud):
    a, b = Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    push(cloud, b, make_report(machine_id="m-B"))
    claim(cloud, a, owner)
    claim(cloud, b, owner)
    assert cloud.get(f"/m/{short(b)}").status_code == 303
    assert _fleet_order(cloud, "m-A", "m-B") == ["m-B", "m-A"]


def test_fleet_never_viewed_keep_label_order_after_viewed(cloud):
    a, b, c = Signer(), Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    for s, name in ((a, "m-A"), (b, "m-B"), (c, "m-C")):
        push(cloud, s, make_report(machine_id=name))
        claim(cloud, s, owner)
    cloud.post(f"/fleet/select/{short(c)}", headers={"HX-Request": "true"})
    assert _fleet_order(cloud, "m-A", "m-B", "m-C") == ["m-C", "m-A", "m-B"]


def test_select_requires_htmx_header(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    claim(cloud, signer, owner)
    assert cloud.post(f"/fleet/select/{short(signer)}").status_code == 403


def test_machine_without_report_renders_no_report_page(cloud, signer):
    owner = login(cloud, "owner@example.com")
    register(cloud, signer, "m-quiet")
    claim(cloud, signer, owner)
    select(cloud, signer)
    for path in ("/", "/health", "/health/faults"):
        r = cloud.get(path)
        assert r.status_code == 200, path
        assert "No report from this machine yet" in r.text
    assert "VMC not initialized" in cloud.get("/status").text
    assert cloud.get("/pill", headers={"HX-Request": "true"}).status_code == 204


def test_selected_machine_pages_render_from_report(cloud, signer):
    own_and_select(cloud, signer, make_report())
    faults = cloud.get("/health/faults")
    assert faults.status_code == 200
    assert "ICE-301" in faults.text
    assert "Remote view" not in faults.text and "m-0042" in faults.text
    # Read-only viewer: no Clear / Record sale / Discard controls.
    assert "Clear" not in faults.text and "Record sale" not in faults.text
    assert "Offline" not in faults.text and "UI version mismatch" not in faults.text
    assert "ICE-301" in cloud.get("/pill").text
    assert "1 active fault" in cloud.get("/").text
    assert "$42.00" in cloud.get("/kpi").text or "42" in cloud.get("/kpi").text
    assert "3/3 OK" in cloud.get("/health").text


def _badge(html):
    """(element, inner text) of the one #context-badge, or (None, None)."""
    from tests.dom_utils import find_by_id, parse_elements

    found = find_by_id(parse_elements(html), "context-badge")
    if not found:
        return None, None
    (el,) = found
    m = re.search(r'id="context-badge".*?>([^<]*)<', html, re.S)
    return el, m.group(1).strip()


def test_machine_pages_show_machine_badge(cloud, signer):
    own_and_select(cloud, signer, make_report())
    for path in ("/", "/health", "/health/faults"):
        r = cloud.get(path)
        el, label = _badge(r.text)
        assert el is not None, path
        assert label == "m-0042", path
        assert el.attrs["href"] == "/fleet"
        assert "title" not in el.attrs
        assert "bg-sky-700" in el.attrs["class"]
        assert "Remote view" not in r.text
        assert "remote-banner" not in r.text


def test_stale_report_turns_badge_offline(cloud, clock, signer):
    own_and_select(cloud, signer, make_report(interval=60))
    clock.advance(180)  # exactly 3 x interval: not yet stale
    el, label = _badge(cloud.get("/health/faults").text)
    assert label == "m-0042" and "bg-amber-500" not in el.attrs["class"]
    clock.advance(181)
    r = cloud.get("/health/faults")
    assert r.status_code == 200
    el, label = _badge(r.text)
    assert label == "m-0042 — offline"
    assert "bg-amber-500" in el.attrs["class"]
    assert "last report from" in el.attrs["title"]
    assert re.search(r"\(\d+m ago\)", el.attrs["title"])
    assert el.attrs["href"] == "/fleet"
    assert "ICE-301" in r.text
    assert "Offline" in cloud.get("/fleet").text
    assert "remote-banner" not in r.text


def test_fleet_page_shows_cloud_badge(cloud, signer):
    login(cloud, "owner@example.com")
    r = cloud.get("/fleet")
    el, label = _badge(r.text)
    assert label == "Cloud" and el.attrs["href"] == "/fleet"
    assert "remote-banner" not in r.text


def test_no_report_page_badge_falls_back_to_machine_label(cloud, signer):
    owner = login(cloud, "owner@example.com")
    register(cloud, signer, "m-quiet")
    claim(cloud, signer, owner)
    select(cloud, signer)
    el, label = _badge(cloud.get("/").text)
    assert label == "m-quiet" and el.attrs["href"] == "/fleet"


def test_login_page_renders_no_badge(cloud):
    r = cloud.get("/login")
    assert r.status_code == 200
    assert "context-badge" not in r.text


def test_admin_page_shows_cloud_badge(cloud):
    login(cloud, "admin@example.com")
    r = cloud.get("/admin")
    assert r.status_code == 200
    el, label = _badge(r.text)
    assert label == "Cloud" and el.attrs["href"] == "/fleet"
    assert "remote-banner" not in r.text


def test_unknown_ui_digest_shows_mismatch_notice_and_installed_ui(cloud, signer):
    own_and_select(cloud, signer, make_report(digest="c" * 64))
    r = cloud.get("/health/faults")
    assert r.status_code == 200
    assert 'id="ui-notice"' in r.text
    assert "UI version mismatch: machine cccccccc, portal " in r.text
    assert "ICE-301" in r.text


def test_matching_ui_digest_has_no_notice(cloud, signer):
    own_and_select(cloud, signer, make_report())
    assert "ui-notice" not in cloud.get("/health/faults").text
    assert "ui-notice" not in cloud.get("/fleet").text


def test_static_serves_installed_ui_and_refuses_escapes(cloud):
    login(cloud, "owner@example.com")
    assert cloud.get("/static/app.css").status_code == 200
    assert cloud.get("/static/../pyproject.toml").status_code == 404
    assert cloud.get("/static/%2e%2e/%2e%2e/pyproject.toml").status_code == 404
    assert cloud.get("/static/..%5c..%5cpyproject.toml").status_code == 404
    assert cloud.get("/static/nope.css").status_code == 404
    assert cloud.get("/static/%00").status_code == 404


def test_unserved_pages_are_not_available_remotely(cloud, signer):
    own_and_select(cloud, signer, make_report())
    for path in ("/controls", "/health/subsystems", "/logout", "/nowhere/at/all"):
        r = cloud.get(path)
        assert r.status_code == 404, path
        assert "Not available remotely yet" in r.text


def test_not_remote_page_renders_without_a_selection(cloud):
    login(cloud, "owner@example.com")
    r = cloud.get("/controls")
    assert r.status_code == 404
    assert "Not available remotely yet" in r.text


def _digest_of(data):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
        return json.loads(tf.extractfile("manifest.json").read())["digest"]


def test_untrusted_bundle_is_never_rendered_or_served(cloud, tmp_path, signer):
    digest, data = marked_bundle(tmp_path, "UNTRUSTED-UI")
    own_and_select(cloud, signer, make_report(digest=digest))
    assert put_bundle(cloud, signer, digest, data).status_code == 201
    r = cloud.get("/health/faults")
    assert "UNTRUSTED-UI" not in r.text
    assert 'id="ui-notice"' in r.text
    assert "UI version mismatch" in r.text
    assert cloud.get("/static/machine-only.txt").status_code == 404


def test_full_page_renders_from_cached_bundle_in_sandbox(
    trusting_cloud, tmp_path, signer
):
    digest, data = marked_bundle(tmp_path, "SANDBOXED-MACHINE-UI")
    cloud = trusting_cloud(digest)
    own_and_select(cloud, signer, make_report(digest=digest))
    assert put_bundle(cloud, signer, digest, data).status_code == 201

    r = cloud.get("/health/faults")
    assert r.status_code == 200
    assert "SANDBOXED-MACHINE-UI" in r.text
    assert "ICE-301" in r.text
    assert "UI version mismatch" not in r.text
    assert "ui-notice" not in r.text
    assert _badge(r.text)[1] == "m-0042"  # the badge still renders in the sandbox
    # Home and health also render through the sandbox, and static follows the bundle.
    assert cloud.get("/health").status_code == 200
    assert cloud.get("/").status_code == 200
    static = cloud.get("/static/machine-only.txt")
    assert static.text == "from the machine UI"
    assert "sandbox" in static.headers["content-security-policy"]


def test_sandbox_blocks_unsafe_template_in_uploaded_bundle(
    trusting_cloud, tmp_path, signer
):
    root = tmp_path / "evil"
    shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
    shutil.copytree(INSTALLED_ROOT / "static", root / "static")
    faults = root / "templates" / "health_faults.html"
    faults.write_text(
        faults.read_text(encoding="utf-8").replace(
            "{% block body %}",
            "{% block body %}{{ request.__class__.__mro__ }}",
            1,
        ),
        encoding="utf-8",
    )
    data = build_bundle(root)
    digest = _digest_of(data)
    cloud = trusting_cloud(digest)
    own_and_select(cloud, signer, make_report(digest=digest))
    put_bundle(cloud, signer, digest, data)
    with pytest.raises(Exception, match="unsafe"):
        cloud.get("/health/faults")


def test_post_to_unserved_path_is_the_same_404_page(cloud):
    login(cloud, "owner@example.com")
    r = cloud.post("/settings/machine")
    assert r.status_code == 404
    assert "Not available remotely yet" in r.text


def test_badge_renders_inside_the_oob_swapped_bar(cloud, signer):
    from tests.dom_utils import find_by_id, parse_elements

    own_and_select(cloud, signer, make_report(digest="c" * 64))
    for path in ("/", "/health", "/health/faults"):
        html = cloud.get(path).text
        els = parse_elements(html)
        (badge,) = find_by_id(els, "context-badge")
        (notice,) = find_by_id(els, "ui-notice")
        (bar,) = find_by_id(els, "bar")
        assert bar.attrs.get("hx-swap-oob") == "true"
        assert badge.is_within("header"), path
        assert notice.is_within("header"), path


def test_fragments_without_selection_do_not_redirect_htmx_polls(cloud):
    login(cloud, "owner@example.com")
    for path in ("/status", "/pill", "/kpi"):
        assert cloud.get(path, headers={"HX-Request": "true"}).status_code == 204
        assert cloud.get(path).status_code == 303


def _home_bundle(tmp_path, body):
    root = tmp_path / "home_probe"
    shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
    shutil.copytree(INSTALLED_ROOT / "static", root / "static")
    home = root / "templates" / "home.html"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            "{% block body %}", "{% block body %}" + body, 1
        ),
        encoding="utf-8",
    )
    data = build_bundle(root)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
        digest = json.loads(tf.extractfile("manifest.json").read())["digest"]
    return digest, data


def test_sandboxed_bundle_cannot_see_request_or_app_state(
    trusting_cloud, tmp_path, signer
):
    digest, data = _home_bundle(
        tmp_path,
        "[[{{ request }}|{{ request is defined }}|"
        "{{ request.app.state if request is defined else 'gone' }}]]",
    )
    cloud = trusting_cloud(digest)
    own_and_select(cloud, signer, make_report(digest=digest))
    assert put_bundle(cloud, signer, digest, data).status_code == 201

    r = cloud.get("/")
    assert r.status_code == 200
    assert "[[|False|gone]]" in r.text
    assert "SnapshotStore" not in r.text
    assert "settings" not in r.text
