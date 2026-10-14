"""The single UI definition lives in ice_ui (templates, static, tiles)."""

from __future__ import annotations

from pathlib import Path

import ice_ui
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS, Permission
from jinja2.sandbox import SandboxedEnvironment

from ice_ui.templating import TEMPLATES_DIR, make_templates
from ice_ui.tiles import TILES, build_tiles
from tests.paths import REPO_ROOT


def test_templates_dir_inside_package():
    assert Path(ice_ui.__file__).parent in TEMPLATES_DIR.parents


def test_sandboxed_environment():
    t = make_templates(TEMPLATES_DIR, sandboxed=True)
    assert isinstance(t.env, SandboxedEnvironment)


def test_base_template_loads_and_filters_registered():
    t = make_templates(TEMPLATES_DIR)
    assert t.get_template("base.html")
    assert "humanize_seconds" in t.env.filters
    assert "sku_url_segment" in t.env.filters


def test_extra_dirs_win(tmp_path):
    (tmp_path / "base.html").write_text("MARKER")
    t = make_templates(TEMPLATES_DIR, extra_dirs=[tmp_path])
    assert t.get_template("base.html").render() == "MARKER"


def test_no_legacy_template_or_static_dirs():
    assert not (REPO_ROOT / "web_interface" / "templates").exists()
    assert not (REPO_ROOT / "web_interface" / "static").exists()


def test_remote_viewer_sees_only_view_status_tiles():
    tiles = build_tiles(REMOTE_VIEWER_PERMISSIONS, {})
    enabled = [t["title"] for t in tiles if t["enabled"]]
    expected = [d.title for d in TILES if Permission.view_status in d.permissions]
    assert enabled == expected == ["Health"]


def test_owner_sees_all_eight_in_order():
    tiles = build_tiles(frozenset(Permission), {})
    assert [t["title"] for t in tiles] == [
        "Health",
        "Products",
        "Inventory",
        "Reports",
        "Controls",
        "Tests",
        "Users",
        "Settings",
    ]
    assert all(t["enabled"] for t in tiles)


def test_lines_fill_context():
    tiles = build_tiles(frozenset(), {"Health": "3 active faults"})
    assert tiles[0]["context"] == "3 active faults"
    assert not any(t["enabled"] for t in tiles)
