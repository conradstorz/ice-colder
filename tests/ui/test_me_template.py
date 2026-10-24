"""Render tests for the shared You page (me.html)."""

from types import SimpleNamespace

from ice_ui.levels import LEVEL_ME
from ice_ui.templating import templates
from ice_ui.views.identity import MachineLabel


def _render(user, **extra):
    ctx = {
        "level": LEVEL_ME,
        "current_user": user,
        "perms": frozenset(),
        "logout_label": "Lock",
        "machine_label": None,
        "permission_lines": [],
    }
    ctx.update(extra)
    return templates.get_template("me.html").render(**ctx)


def _bar(html):
    """Return the top bar markup only (from id="bar" to its closing tag)."""
    start = html.index('id="bar"')
    end = html.index("</header>", start)
    return html[start:end]


def test_edge_shape():
    user = SimpleNamespace(
        name="Pat",
        role_label="owner",
        email="pat@example.com",
        last_login_at="2026-10-23 09:00",
    )
    lines = ["See status", "Edit the catalog", "Collect cash"]
    html = _render(
        user,
        machine_label=MachineLabel("Lobby Ice", "ice-07"),
        permission_lines=lines,
    )
    assert "Pat" in html
    assert "pat@example.com" in html
    assert "<strong>owner</strong> on Lobby Ice (ice-07)" in html
    for line in lines:
        assert line in html
    assert "Last signed in" in html
    assert 'hx-post="/logout"' in html
    assert ">Lock</button>" in html


def test_cloud_shape_has_no_last_login():
    user = SimpleNamespace(name="Sam", role_label="tech", email=None)
    html = _render(
        user,
        machine_label=MachineLabel("Lobby Ice", "ice-07"),
        logout_label="Sign out",
        permission_lines=["See status"],
    )
    assert "<strong>tech</strong> on Lobby Ice (ice-07)" in html
    assert 'hx-post="/logout"' in html
    assert ">Sign out</button>" in html
    assert "Last signed in" not in html


def test_no_email_renders_no_address():
    user = SimpleNamespace(name="Sam", role_label="tech", email=None)
    html = _render(user, machine_label=MachineLabel("Lobby Ice", "ice-07"))
    assert "@" not in html


def test_no_machine_selected():
    user = SimpleNamespace(name="Sam", role_label=None, email=None)
    html = _render(user)
    assert "No machine selected" in html
    assert 'href="/fleet"' in html
    assert "Role:" not in html
    assert " on Lobby" not in html


def test_role_without_machine_shows_role_line():
    user = SimpleNamespace(name="Pat", role_label="owner", email=None)
    html = _render(user, machine_label=None)
    assert "Role: <strong>owner</strong>" in html
    assert "No machine selected" not in html


def test_empty_permissions():
    user = SimpleNamespace(name="Sam", role_label="tech", email=None)
    assert "Nothing on this page yet." in _render(user)


def test_bar_chip_and_crumb():
    user = SimpleNamespace(name="Pat", role_label="owner", email=None)
    bar = _bar(_render(user))
    assert 'href="/me"' in bar
    assert 'aria-label="Signed in as Pat"' in bar
    assert 'aria-current="page">You</span>' in bar
