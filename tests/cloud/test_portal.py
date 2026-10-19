import gzip
import io
import json
import shutil
import tarfile

import pytest

from ice_ui.bundle import INSTALLED_ROOT, build_bundle
from tests.cloud.conftest import push, select
from tests.cloud.reports import FP, make_report

PAGES = ["/", "/status", "/pill", "/kpi", "/health", "/health/faults"]


def test_no_cookie_redirects_every_machine_page_to_fleet(cloud):
    for path in PAGES:
        r = cloud.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/fleet"


def test_cookie_naming_unreported_machine_redirects_to_fleet(cloud):
    cloud.cookies.set("ice_cloud_machine", "m-ghost")
    assert cloud.get("/").status_code == 303


def test_fleet_lists_machines_and_select_sets_a_non_credential_cookie(cloud):
    push(cloud, make_report())
    page = cloud.get("/fleet")
    assert page.status_code == 200
    assert "m-0042" in page.text and "Online" in page.text
    assert "1 active fault" in page.text
    assert f"/fleet/select/{FP}" in page.text

    r = cloud.post(f"/fleet/select/{FP}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert f"ice_cloud_machine={FP}" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_select_unknown_or_malformed_machine_is_404(cloud):
    assert cloud.post("/fleet/select/m-ghost").status_code == 404
    assert cloud.post("/fleet/select/bad%20id").status_code == 404


def test_selected_machine_pages_render_from_report(cloud):
    push(cloud, make_report())
    select(cloud)
    faults = cloud.get("/health/faults")
    assert faults.status_code == 200
    assert "ICE-301" in faults.text
    assert "Remote view" in faults.text and "m-0042" in faults.text
    # Read-only viewer: no Clear / Record sale / Discard controls.
    assert "Clear" not in faults.text and "Record sale" not in faults.text
    assert "Offline" not in faults.text and "UI version mismatch" not in faults.text
    assert "ICE-301" in cloud.get("/pill").text
    assert "1 active fault" in cloud.get("/").text
    assert "$42.00" in cloud.get("/kpi").text or "42" in cloud.get("/kpi").text
    assert "3/3 OK" in cloud.get("/health").text


def test_stale_machine_still_renders_under_offline_banner(cloud, clock):
    push(cloud, make_report(interval=60))
    select(cloud)
    clock.advance(180)  # exactly 3 x interval: not yet stale
    assert "Offline" not in cloud.get("/health/faults").text
    clock.advance(181)
    r = cloud.get("/health/faults")
    assert r.status_code == 200
    assert "Offline — showing last report" in r.text
    assert "6m" in r.text  # 181 s past 3 x interval = 361 s old
    assert "ICE-301" in r.text
    assert "Offline" in cloud.get("/fleet").text


def test_unknown_ui_digest_shows_mismatch_banner_and_installed_ui(cloud):
    push(cloud, make_report(digest="c" * 64))
    select(cloud)
    r = cloud.get("/health/faults")
    assert r.status_code == 200
    assert "UI version mismatch: machine cccccccc" in r.text
    assert "ICE-301" in r.text


def test_static_serves_installed_ui_and_refuses_escapes(cloud):
    assert cloud.get("/static/app.css").status_code == 200
    assert cloud.get("/static/../pyproject.toml").status_code == 404
    assert cloud.get("/static/%2e%2e/%2e%2e/pyproject.toml").status_code == 404
    assert cloud.get("/static/..%5c..%5cpyproject.toml").status_code == 404
    assert cloud.get("/static/nope.css").status_code == 404
    assert cloud.get("/static/%00").status_code == 404


def test_unserved_pages_are_not_available_remotely(cloud):
    push(cloud, make_report())
    select(cloud)
    for path in ("/controls", "/health/subsystems", "/logout", "/nowhere/at/all"):
        r = cloud.get(path)
        assert r.status_code == 404, path
        assert "Not available remotely yet" in r.text


def test_not_remote_page_renders_without_a_selection(cloud):
    r = cloud.get("/controls")
    assert r.status_code == 404
    assert "Not available remotely yet" in r.text


def _marked_bundle(tmp_path, marker):
    root = tmp_path / "ui_copy"
    shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
    shutil.copytree(INSTALLED_ROOT / "static", root / "static")
    (root / "static" / "machine-only.txt").write_text("from the machine UI")
    faults = root / "templates" / "health_faults.html"
    faults.write_text(
        faults.read_text(encoding="utf-8").replace(
            "{% block body %}", "{% block body %}<!-- " + marker + " -->", 1
        ),
        encoding="utf-8",
    )
    data = build_bundle(root)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
        digest = json.loads(tf.extractfile("manifest.json").read())["digest"]
    return digest, data


def test_full_page_renders_from_cached_bundle_in_sandbox(cloud, tmp_path):
    digest, data = _marked_bundle(tmp_path, "SANDBOXED-MACHINE-UI")
    assert cloud.put(f"/api/v1/ui-bundles/{digest}", content=data).status_code == 201
    push(cloud, make_report(digest=digest))
    select(cloud)

    r = cloud.get("/health/faults")
    assert r.status_code == 200
    assert "SANDBOXED-MACHINE-UI" in r.text
    assert "ICE-301" in r.text
    assert "UI version mismatch" not in r.text
    assert "Remote view" in r.text  # banner include still works in the sandbox
    # Home and health also render through the sandbox, and static follows the bundle.
    assert cloud.get("/health").status_code == 200
    assert cloud.get("/").status_code == 200
    assert cloud.get("/static/machine-only.txt").text == "from the machine UI"


def test_sandbox_blocks_unsafe_template_in_uploaded_bundle(cloud, tmp_path):
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
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
        digest = json.loads(tf.extractfile("manifest.json").read())["digest"]
    cloud.put(f"/api/v1/ui-bundles/{digest}", content=data)
    push(cloud, make_report(digest=digest))
    select(cloud)
    with pytest.raises(Exception, match="unsafe"):
        cloud.get("/health/faults")


def test_post_to_unserved_path_is_the_same_404_page(cloud):
    r = cloud.post("/logout")
    assert r.status_code == 404
    assert "Not available remotely yet" in r.text


def test_banner_renders_inside_the_oob_swapped_bar(cloud):
    from tests.dom_utils import find_by_id, parse_elements

    push(cloud, make_report())
    select(cloud)
    for path in ("/", "/health", "/health/faults"):
        html = cloud.get(path).text
        els = parse_elements(html)
        (banner,) = find_by_id(els, "remote-banner")
        (bar,) = find_by_id(els, "bar")
        assert bar.attrs.get("hx-swap-oob") == "true"
        assert banner.is_within("header"), path


def test_fragments_without_selection_do_not_redirect_htmx_polls(cloud):
    for path in ("/status", "/pill", "/kpi"):
        assert cloud.get(path, headers={"HX-Request": "true"}).status_code == 204
        assert cloud.get(path).status_code == 303
