"""Tests for the Controls level (Task 10): restart, reset, shutdown with two-tap confirm."""

import pytest
from services.access import Role, ROLE_PERMISSIONS, Permission
from web_interface import context


class TestControlsPermissionGates:
    """GET /controls is 200 for owner and tech, 403 for secretary and loader."""

    @pytest.mark.parametrize("role", list(Role))
    def test_get_controls_permission_matrix(self, login_as, role):
        """Machine_controls is held by owner and tech only."""
        client = login_as(role)
        resp = client.get("/controls")
        if Permission.machine_controls in ROLE_PERMISSIONS[role]:
            assert resp.status_code == 200, (role, resp.status_code)
        else:
            assert resp.status_code == 403, (role, resp.status_code)

    def test_owner_can_get_controls(self, login_as):
        client = login_as(Role.owner)
        resp = client.get("/controls")
        assert resp.status_code == 200

    def test_tech_can_get_controls(self, login_as):
        client = login_as(Role.tech)
        resp = client.get("/controls")
        assert resp.status_code == 200

    def test_secretary_cannot_get_controls(self, login_as):
        client = login_as(Role.secretary, name="Secretary")
        resp = client.get("/controls")
        assert resp.status_code == 403

    def test_loader_cannot_get_controls(self, login_as):
        client = login_as(Role.loader, name="Loader")
        resp = client.get("/controls")
        assert resp.status_code == 403


class TestControlsInitialRender:
    """The initial render must contain no attribute that fires the command."""

    def test_initial_render_contains_no_post_attribute(self, client):
        """The first tap must `hx-get` the confirm variant, not `hx-post` the command."""
        resp = client.get("/controls")
        assert resp.status_code == 200

        # The response must NOT contain hx-post pointing at /controls/restart, etc.
        # It MUST contain hx-get pointing at /controls/confirm/...
        for cmd in ["restart", "reset", "shutdown"]:
            # Verify no direct post
            assert f'hx-post="/controls/{cmd}"' not in resp.text, (
                f"Initial render contains hx-post for {cmd}"
            )
            # Verify the get to confirm endpoint exists
            assert f'hx-get="/controls/confirm/{cmd}"' in resp.text, (
                f"Initial render missing hx-get to confirm for {cmd}"
            )

    def test_initial_render_shows_all_three_buttons(self, client):
        """All three command buttons are rendered."""
        resp = client.get("/controls")
        assert resp.status_code == 200
        assert "Restart" in resp.text
        assert "Reset" in resp.text
        assert "Shutdown" in resp.text

    def test_initial_render_shows_three_button_wrappers(self, client):
        """Each button has its own wrapper with a unique ID."""
        resp = client.get("/controls")
        assert resp.status_code == 200
        assert 'id="confirm-restart"' in resp.text
        assert 'id="confirm-reset"' in resp.text
        assert 'id="confirm-shutdown"' in resp.text


class TestConfirmEndpoint:
    """GET /controls/confirm/{command} returns the confirm variant."""

    def test_confirm_endpoint_requires_machine_controls(self, login_as):
        client = login_as(Role.secretary, name="Secretary")
        resp = client.get("/controls/confirm/restart")
        assert resp.status_code == 403

    def test_confirm_with_confirming_true_renders_confirm_state(self, client):
        """GET /controls/confirm/restart?confirming=true renders Confirm and Cancel buttons."""
        resp = client.get("/controls/confirm/restart?confirming=true")
        assert resp.status_code == 200
        # Confirm button with hx-post
        assert 'hx-post="/controls/restart"' in resp.text
        assert "Restart Machine" in resp.text
        # Cancel button with hx-get back to confirm endpoint
        assert 'hx-get="/controls/confirm/restart"' in resp.text
        assert 'hx-vals=\'{"confirming": "false"}\'' in resp.text
        assert "Cancel" in resp.text

    def test_confirm_with_confirming_false_renders_initial_button(self, client):
        """GET /controls/confirm/restart?confirming=false renders the initial button."""
        resp = client.get("/controls/confirm/restart?confirming=false")
        assert resp.status_code == 200
        # Initial button with hx-get to confirm (not hx-post)
        assert 'hx-get="/controls/confirm/restart"' in resp.text
        assert "Restart" in resp.text
        # Must NOT contain the hx-post or confirm button text
        assert 'hx-post="/controls/restart"' not in resp.text
        assert "Restart Machine" not in resp.text

    def test_confirm_without_confirming_param_defaults_to_true(self, client):
        """GET /controls/confirm/restart (no param) defaults to confirming=true."""
        resp = client.get("/controls/confirm/restart")
        assert resp.status_code == 200
        # Should render confirm variant
        assert 'hx-post="/controls/restart"' in resp.text
        assert "Restart Machine" in resp.text


class TestPostCommands:
    """POST /controls/{command} executes the command."""

    def test_post_restart_returns_200(self, client):
        resp = client.post("/controls/restart")
        assert resp.status_code == 200
        assert "Restart command sent" in resp.text

    def test_post_shutdown_returns_200(self, client):
        resp = client.post("/controls/shutdown")
        assert resp.status_code == 200
        assert "Shutdown command sent" in resp.text

    def test_post_reset_with_no_vmc_returns_error_message(self, client):
        """When vmc_instance is None, reset fails gracefully."""
        assert context.vmc_instance is not None  # The fixture wires one
        resp = client.post("/controls/reset")
        # The wired VMC is in idle, not error
        assert resp.status_code == 200
        assert (
            "Reset ignored" in resp.text
            or "Reset complete" in resp.text
            or "Reset failed" in resp.text
        )

    def test_post_unknown_command_does_not_500(self, client):
        resp = client.post("/controls/unknown")
        assert resp.status_code == 200
        assert "Unknown command" in resp.text

    def test_post_requires_htmx_header(self, login_as):
        """POST without HX-Request header is 403."""
        client = login_as(Role.owner)
        # Remove the HX-Request header by making a new request without it
        # The TestClient adds it by default, so we need to make a raw request
        from fastapi.testclient import TestClient
        from web_interface.server import app
        import web_interface.auth as web_auth

        raw_client = TestClient(app)
        # Copy cookies from the authenticated client
        raw_client.cookies.set(
            web_auth.DEVICE_COOKIE, client.cookies.get(web_auth.DEVICE_COOKIE)
        )
        raw_client.cookies.set(
            web_auth.SESSION_COOKIE, client.cookies.get(web_auth.SESSION_COOKIE)
        )
        # Explicitly do NOT set HX-Request header
        resp = raw_client.post("/controls/restart")
        assert resp.status_code == 403

    def test_post_loader_gets_403_even_with_htmx_header(self, login_as):
        """Loader is 403 even with HX-Request header."""
        client = login_as(Role.loader, name="Loader")
        # The client already has HX-Request header set
        resp = client.post("/controls/restart")
        assert resp.status_code == 403

    def test_post_secretary_gets_403_even_with_htmx_header(self, login_as):
        """Secretary is 403 even with HX-Request header."""
        client = login_as(Role.secretary, name="Secretary")
        # The client already has HX-Request header set
        resp = client.post("/controls/restart")
        assert resp.status_code == 403

    def test_post_owner_can_execute_commands(self, client):
        """Owner can execute all commands."""
        for cmd in ["restart", "reset", "shutdown"]:
            resp = client.post(f"/controls/{cmd}")
            assert resp.status_code == 200

    def test_post_tech_can_execute_commands(self, login_as):
        """Tech can execute all commands."""
        client = login_as(Role.tech)
        for cmd in ["restart", "reset", "shutdown"]:
            resp = client.post(f"/controls/{cmd}")
            assert resp.status_code == 200


class TestResetStateLogic:
    """Reset has special state logic in fsm_control.perform_command."""

    def test_reset_with_vmc_not_in_error_state(self, client):
        """Reset is ignored when VMC is not in error state."""
        vmc = context.vmc_instance
        # Verify it's not in error
        assert vmc.state != "error"

        resp = client.post("/controls/reset")
        assert resp.status_code == 200
        # Reset returns a message based on VMC state


class TestConfirmUrlRoundTrip:
    """The two-tap confirm flow round-trip works correctly."""

    def test_tap_one_fetches_confirm_button(self, client):
        """First GET to /controls/confirm/{command} shows Confirm and Cancel."""
        resp = client.get("/controls/confirm/restart")
        assert resp.status_code == 200
        assert 'hx-post="/controls/restart"' in resp.text
        assert "Cancel" in resp.text

    def test_tap_two_posts_the_command(self, client):
        """POST to /controls/{command} executes and returns result."""
        resp = client.post("/controls/restart")
        assert resp.status_code == 200
        assert "Restart command sent" in resp.text

    def test_cancel_returns_to_initial_button(self, client):
        """GET /controls/confirm/{command}?confirming=false returns initial button."""
        resp = client.get("/controls/confirm/restart?confirming=false")
        assert resp.status_code == 200
        assert 'hx-post="/controls/restart"' not in resp.text
        assert 'hx-get="/controls/confirm/restart"' in resp.text


class TestConfirmEndpointReturnsFragment:
    """GET /controls/confirm/{command} must return only the button
    fragment, never a full document — the confirm endpoint's response is
    swapped in via hx-target/hx-swap="outerHTML" into the existing page,
    so a full <html>/<body>/<main> document would nest a second <main>
    inside the page's own on every confirm and Cancel tap (Task 10 review
    round 1). Substring checks alone can't catch this since the wanted
    strings are also present inside the (unwanted) full document, so these
    assert on the absence of document-level structure too.
    """

    def test_confirm_variant_is_a_fragment_not_a_document(self, client):
        resp = client.get("/controls/confirm/restart?confirming=true")
        assert resp.status_code == 200
        lowered = resp.text.lower()
        assert "<!doctype" not in lowered
        assert "<html" not in lowered
        assert "<head" not in lowered
        assert "<body" not in lowered
        assert "<main" not in lowered
        # Still the exact button fragment the caller needs to swap in.
        assert 'id="confirm-restart"' in resp.text
        assert 'hx-post="/controls/restart"' in resp.text

    def test_cancel_variant_is_a_fragment_not_a_document(self, client):
        resp = client.get("/controls/confirm/restart?confirming=false")
        assert resp.status_code == 200
        lowered = resp.text.lower()
        assert "<!doctype" not in lowered
        assert "<html" not in lowered
        assert "<head" not in lowered
        assert "<body" not in lowered
        assert "<main" not in lowered
        assert 'id="confirm-restart"' in resp.text
        assert 'hx-get="/controls/confirm/restart"' in resp.text


class TestInlineFeedback:
    """Result messages replace the button area inline."""

    def test_post_result_is_inline_paragraph(self, client):
        """POST result is wrapped in a <p> tag for inline replacement."""
        resp = client.post("/controls/restart")
        assert resp.status_code == 200
        assert resp.text.startswith("<p>")
        assert resp.text.endswith("</p>")
        assert "Restart command sent" in resp.text

    def test_unknown_command_returns_inline_message(self, client):
        """Unknown command also returns inline message."""
        resp = client.post("/controls/xyz")
        assert resp.status_code == 200
        assert resp.text.startswith("<p>")
        assert "Unknown command" in resp.text

    def test_unknown_command_is_html_escaped(self, client):
        """Copilot review (PR 20, comment 4113241294): fsm_control's
        unknown-command branch echoes the raw {command} path parameter
        back into its message, and this route used to embed that message
        into an HTMLResponse via an f-string, bypassing Jinja
        auto-escaping. /controls/<markup> must not be able to inject
        markup into the HTMX target the response is swapped into."""
        resp = client.post("/controls/%3Cimg%20src=x%20onerror=alert(1)%3E")
        assert resp.status_code == 200
        assert "<img" not in resp.text
        assert "&lt;img" in resp.text
