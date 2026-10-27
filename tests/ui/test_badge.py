"""The context badge and ui-notice strip in base.html's #bar header."""

from types import SimpleNamespace

import pytest

from ice_ui.levels import LEVEL_HEALTH_FAULTS
from ice_ui.templating import TEMPLATES_DIR, make_templates, templates


def _badge(**kw):
    base = dict(
        label="Lobby Ice", url="/fleet", tone="remote", title=None, external=False
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _render(env=templates, **ctx) -> str:
    return env.get_template("base.html").render(level=LEVEL_HEALTH_FAULTS, **ctx)


def _badge_el(html):
    from tests.dom_utils import find_by_id, parse_elements

    (el,) = find_by_id(parse_elements(html), "context-badge")
    return el


def test_no_badge_without_variable():
    assert "context-badge" not in _render()


def test_badge_is_inside_the_oob_swapped_bar():
    """Boosted navigation swaps #bar out-of-band on every response, so a
    badge inside it is replaced (never stacked) and dropped when absent."""
    from tests.dom_utils import find_by_id, parse_elements

    html = _render(badge=_badge())
    els = parse_elements(html)
    (badge,) = find_by_id(els, "context-badge")
    (bar,) = find_by_id(els, "bar")
    assert bar.attrs.get("hx-swap-oob") == "true"
    assert badge.is_within("header")
    assert html.index('<header id="bar"') < html.index("context-badge")
    assert html.index("context-badge") < html.index("</header>")
    assert html.count('id="context-badge"') == 1


def test_badge_with_url_renders_anchor():
    html = _render(badge=_badge())
    el = _badge_el(html)
    assert el.tag == "a"
    assert el.attrs.get("href") == "/fleet"
    assert "hx-boost" not in el.attrs
    assert "Lobby Ice" in html


def test_badge_external_link_disables_boost():
    url = "https://cloud.example/m/abcd-efgh-ijkl-mnop"
    el = _badge_el(_render(badge=_badge(url=url, external=True)))
    assert el.tag == "a"
    assert el.attrs.get("href") == url
    assert el.attrs.get("hx-boost") == "false"


def test_badge_without_url_is_plain_span():
    el = _badge_el(_render(badge=_badge(url=None)))
    assert el.tag == "span"
    assert "href" not in el.attrs


@pytest.mark.parametrize(
    "tone,fragments",
    [
        ("local", ["bg-slate-600", "text-white"]),
        ("remote", ["bg-sky-700", "text-white"]),
        ("cloud", ["bg-sky-700", "text-white"]),
        ("offline", ["bg-amber-500", "text-amber-950"]),
    ],
)
def test_badge_tones(tone, fragments):
    classes = _badge_el(_render(badge=_badge(tone=tone))).attrs.get("class", "")
    for fragment in fragments:
        assert fragment in classes


def test_badge_title_attribute():
    title = "last report from 2026-10-13 14:05 (12m ago)"
    el = _badge_el(_render(badge=_badge(title=title)))
    assert el.attrs.get("title") == title


@pytest.mark.parametrize("url", ["/fleet", None])
def test_badge_truncates_and_titles_with_label(url):
    long_label = "A very long operator entered machine name " * 4
    el = _badge_el(_render(badge=_badge(label=long_label, url=url, title=None)))
    classes = el.attrs.get("class", "").split()
    assert "truncate" in classes
    assert "max-w-[10rem]" in classes
    assert el.attrs.get("title") == long_label


def test_badge_label_is_escaped():
    html = _render(badge=_badge(label="<script>alert(1)</script>"))
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)" not in html


def test_ui_notice_strip():
    from tests.dom_utils import find_by_id, parse_elements

    text = "UI version mismatch: machine a1b2c3d4, portal e5f6a7b8"
    html = _render(ui_notice=text)
    (notice,) = find_by_id(parse_elements(html), "ui-notice")
    assert notice.is_within("header")
    assert text in html
    assert "ui-notice" not in _render()


def test_remote_banner_is_gone():
    legacy = SimpleNamespace(machine_id="m-0042", stale=True, ui_mismatch=True)
    assert "remote-banner" not in _render(remote=legacy)


@pytest.mark.parametrize("sandboxed", [False, True])
def test_badge_renders_in_either_environment(sandboxed):
    env = make_templates(TEMPLATES_DIR, sandboxed=sandboxed)
    html = _render(env, badge=_badge(), ui_notice="hello notice")
    assert 'id="context-badge"' in html
    assert "hello notice" in html
