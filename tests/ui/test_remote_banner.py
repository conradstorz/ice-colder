"""partials/remote_banner.html, included by base.html only when `remote` is set."""

from types import SimpleNamespace

import pytest

from ice_ui.levels import LEVEL_HEALTH_FAULTS
from ice_ui.templating import TEMPLATES_DIR, make_templates, templates

DIGEST_A = "a1b2c3d4" + "0" * 56
DIGEST_B = "e5f6a7b8" + "1" * 56


def _remote(**kw):
    base = dict(
        machine_id="m-0042",
        last_report_local="2026-10-13 14:05",
        age_text="12m",
        stale=False,
        ui_mismatch=False,
        report_digest=DIGEST_A,
        portal_digest=DIGEST_B,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _render(env=templates, **ctx) -> str:
    return env.get_template("base.html").render(level=LEVEL_HEALTH_FAULTS, **ctx)


def test_no_banner_without_remote():
    assert "remote-banner" not in _render()


def test_banner_is_first_child_of_body():
    html = _render(remote=_remote())
    body_open = html.index("<body")
    body_tag_end = html.index(">", html.index("hx-swap", body_open)) + 1
    assert html[body_tag_end:].lstrip().startswith('<div id="remote-banner"')
    assert html.index("remote-banner") < html.index('<header id="bar"')


def test_fresh_report_shows_machine_only():
    html = _render(remote=_remote())
    assert "m-0042" in html
    assert "Offline" not in html
    assert "UI version mismatch" not in html


def test_stale_banner_text():
    html = _render(remote=_remote(stale=True))
    assert "Offline — showing last report from 2026-10-13 14:05 (12m ago)" in html


def test_ui_mismatch_banner_text_uses_first_eight_hex_chars():
    html = _render(remote=_remote(ui_mismatch=True))
    assert "UI version mismatch: machine a1b2c3d4, portal e5f6a7b8" in html
    assert DIGEST_A not in html


def test_both_notices_can_show_together():
    html = _render(remote=_remote(stale=True, ui_mismatch=True))
    assert "Offline" in html
    assert "UI version mismatch" in html


@pytest.mark.parametrize("sandboxed", [False, True])
def test_banner_renders_in_either_environment(sandboxed):
    env = make_templates(TEMPLATES_DIR, sandboxed=sandboxed)
    html = _render(env, remote=_remote(stale=True, ui_mismatch=True))
    assert "UI version mismatch: machine a1b2c3d4, portal e5f6a7b8" in html
