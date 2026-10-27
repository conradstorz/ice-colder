"""base.html renders byte-identically when no ``remote`` context is passed.

The golden file was rendered from base.html *before* the remote-banner include
was added (cloud spec section 4), so this proves the machine's own pages are
unchanged by it. The file is marked ``-text`` in .gitattributes so a CRLF
checkout cannot alter its bytes. Regenerated for the titlebar badge change
(``relative`` on the bar header, whitespace of the ``ui_notice`` block); with
no ``badge``/``ui_notice`` the bar carries no other new markup.
"""

from pathlib import Path

from ice_ui.levels import LEVEL_HEALTH_FAULTS
from ice_ui.templating import templates

GOLDEN = Path(__file__).parent / "golden" / "base_no_remote.html"


def _render(**ctx) -> bytes:
    html = templates.get_template("base.html").render(level=LEVEL_HEALTH_FAULTS, **ctx)
    return html.encode("utf-8")


def test_base_without_remote_matches_golden():
    assert _render() == GOLDEN.read_bytes()


def test_base_with_remote_none_matches_golden():
    assert _render(remote=None) == GOLDEN.read_bytes()
