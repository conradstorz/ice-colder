"""The signed-in chip in base.html's #bar."""

from types import SimpleNamespace

from ice_ui.levels import LEVEL_HEALTH_FAULTS
from ice_ui.templating import templates


def _bar(**ctx) -> str:
    html = templates.get_template("base.html").render(level=LEVEL_HEALTH_FAULTS, **ctx)
    start = html.index('id="bar"')
    return html[start : html.index("</header>", start)]


def test_chip_with_role_and_name():
    bar = _bar(current_user=SimpleNamespace(name="Ada", role_label="owner"))
    assert 'href="/me"' in bar
    assert ">owner</span>" in bar
    assert "Ada" in bar
    assert 'max-md:hidden text-sm text-slate-200 truncate max-w-[10rem]">Ada' in bar


def test_chip_without_role_label():
    bar = _bar(current_user=SimpleNamespace(name="Ada", role_label=None))
    assert 'href="/me"' in bar
    assert "Ada" in bar
    assert "bg-slate-600" not in bar


def test_no_chip_without_user():
    assert "/me" not in _bar()
