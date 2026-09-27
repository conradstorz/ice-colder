"""Tests for the Tests placeholder level (Task 11): GET /tests.

Tests cover: permission gates (run_tests for owner/tech, 403 for secretary/loader),
rendering inside the shell with the correct breadcrumb (Home › Tests), no forms
or test actions in the body, and placeholder copy describing future subsystem tests.

Fixtures come from tests/conftest.py.
"""

import pytest
from re import DOTALL, search as re_search
from services.access import Role


class TestTestsPermissionGate:
    """GET /tests gates on run_tests: 200 for owner/tech, 403 for secretary/loader."""

    @pytest.mark.parametrize(
        "role,expected_status",
        [
            (Role.owner, 200),
            (Role.tech, 200),
            (Role.secretary, 403),
            (Role.loader, 403),
        ],
    )
    def test_tests_gate(self, login_as, role, expected_status):
        """Verify permission gate matches run_tests access."""
        client = login_as(role)
        response = client.get("/tests")
        assert response.status_code == expected_status


class TestTestsShellRendering:
    """GET /tests renders inside the shell with the correct breadcrumb."""

    def test_renders_inside_shell(self, client):
        """Page extends base.html and renders within the shell."""
        response = client.get("/tests")
        assert response.status_code == 200
        # Check for shell elements
        assert "<header" in response.text
        assert "<main" in response.text
        assert 'id="bar"' in response.text

    def test_breadcrumb_home_tests(self, client):
        """Breadcrumb shows Home › Tests, with Tests unlinked."""
        response = client.get("/tests")
        assert response.status_code == 200
        # The breadcrumb nav renders Home as a link and Tests as plain text (current level)
        assert ">Home<" in response.text
        assert ">Tests<" in response.text
        # Tests should appear after Home in the breadcrumb area
        text = response.text
        home_pos = text.find(">Home<")
        tests_pos = text.find(">Tests<")
        assert home_pos > 0 and tests_pos > home_pos, (
            "Tests should appear after Home in breadcrumb"
        )

    def test_title_tag(self, client):
        """Page <title> includes Tests."""
        response = client.get("/tests")
        assert response.status_code == 200
        assert "<title>" in response.text
        assert "Tests" in response.text


class TestTestsBodyContent:
    """GET /tests renders placeholder text about future subsystem tests."""

    def test_placeholder_text_present(self, client):
        """Body contains the placeholder message."""
        response = client.get("/tests")
        assert response.status_code == 200
        assert "Subsystem tests will arrive in a later release" in response.text

    def test_heading_present(self, client):
        """Body contains Tests heading."""
        response = client.get("/tests")
        assert response.status_code == 200
        assert "<h1" in response.text
        assert ">Tests<" in response.text


class TestTestsNoActionsMixin:
    """GET /tests must contain no test actions: no forms, no hx-post in the body.

    Scoped to <main> body to exclude shell bar elements (pill hx-get, Lock hx-post).
    """

    def _extract_main_body(self, html: str) -> str:
        """Extract content between <main> and </main> tags.

        Fails loudly (raises) rather than returning "" when the body
        cannot be located: a helper whose failure mode is "return
        something every containment assertion passes against" turns a
        broken extraction into a hollow, always-green test suite. A
        previous version passed a numeric `flags=8` (re.MULTILINE)
        believing it to be re.DOTALL (which is 16), so `(.*)` could
        never cross the newlines between `<main ...>` and `</main>`;
        re_search always returned None and every "no actions"
        assertion below silently checked `"X" not in ""`.
        """
        match = re_search(r"<main[^>]*>(.*)</main>", html, flags=DOTALL)
        assert match is not None, (
            "Could not locate <main>...</main> in the response body; "
            "the no-actions assertions below cannot be trusted without it"
        )
        return match.group(1)

    def test_extract_main_body_returns_real_content(self, client):
        """Positive case: the helper must return the real body, not "".

        Guards directly against the regression described above: proves
        the extraction spans the newlines between the placeholder
        heading and the closing </main> tag and yields non-empty,
        recognizable content, rather than merely being exercised
        indirectly through assertions that would also pass on "".
        """
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert body != "", "Extracted main body must not be empty"
        assert "Subsystem tests will arrive in a later release" in body
        assert "<h1" in body

    def test_no_form_in_body(self, client):
        """Body contains no <form> elements."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert "<form" not in body, "Tests level must contain no forms"

    def test_no_hx_post_in_body(self, client):
        """Body contains no hx-post attributes."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert "hx-post" not in body, "Tests level must contain no hx-post in body"

    def test_no_hx_get_in_body(self, client):
        """Body contains no hx-get attributes (the shell bar has one for the pill)."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert "hx-get" not in body, "Tests level must contain no hx-get in body"

    def test_bar_still_has_expected_attributes(self, client):
        """Verify bar (outside main) has its expected HTMX attributes.

        This confirms our main-body extraction is working; if it weren't,
        we'd miss the bar's attributes and this would fail.
        """
        response = client.get("/tests")
        assert response.status_code == 200
        # The bar should have Lock button with hx-post and pill with hx-get
        assert 'hx-post="/logout"' in response.text, (
            "Bar must have Lock button with hx-post"
        )
        assert 'hx-get="/pill"' in response.text, (
            "Bar must have health pill with hx-get"
        )


class TestTestsLevel:
    """Tests level integration: structure, styling, no accidental functionality."""

    def test_white_card_container(self, client):
        """Placeholder text is in a styled card container."""
        response = client.get("/tests")
        assert response.status_code == 200
        # Check for card styling in the body
        body = response.text[
            response.text.find("<main") : response.text.find("</main>")
        ]
        assert "bg-white" in body or "rounded-xl" in body, (
            "Tests should use card styling"
        )

    def test_no_polling_endpoints(self, client):
        """Body contains no polling trigger attributes (no every Xs)."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = response.text[
            response.text.find("<main") : response.text.find("</main>")
        ]
        assert "every " not in body, "Tests must not use polling triggers"
