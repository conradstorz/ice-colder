"""Tests for the Tests level's shell rendering: GET /tests.

Originally written for Task 11's placeholder body ("Subsystem tests will
arrive in a later release", no forms or hx-post anywhere in <main>). Task
13a replaced that placeholder with the real discovery UI (subsystem cards,
a Run-all button, Simulated sale/Test log tiles, an End/Take-over lease
banner) -- exactly what this file's own docstring said would eventually
happen. The permission gate, shell-rendering, breadcrumb and title-tag
tests below are unaffected by that and still hold; the placeholder-content
and no-hx-post assertions that described the OLD, intentionally-empty body
have been updated to describe the new one instead (see
tests/test_routes_tests.py for the full, detailed coverage of Task 13a's
actual content -- this file stays focused on the shell-level concerns its
docstring originally promised).

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
    """GET /tests renders the real discovery content (Task 13a)."""

    def test_real_content_present(self, client):
        """Body contains the Run-all action and the Test log tile -- the
        placeholder copy ("Subsystem tests will arrive in a later
        release") this test used to check for is gone, superseded by
        Task 13a per this file's own module docstring."""
        response = client.get("/tests")
        assert response.status_code == 200
        assert "Run all" in response.text
        assert "Test log" in response.text
        assert "Subsystem tests will arrive in a later release" not in response.text

    def test_heading_present(self, client):
        """Body contains Tests heading."""
        response = client.get("/tests")
        assert response.status_code == 200
        assert "<h1" in response.text
        assert ">Tests<" in response.text


class TestTestsNoActionsMixin:
    """GET /tests scoped to <main>, excluding shell bar elements (pill
    hx-get, Lock hx-post) that every level's body legitimately renders
    around.

    Originally asserted the body had NO hx-post/hx-get/form at all, back
    when /tests was Task 11's inert placeholder. Task 13a gives it real
    actions (Run-all, End, Take over all POST; the subsystem cards and
    the Simulated sale/Test log tiles are plain <a> links, and no <form>
    lives on THIS page -- per-command forms are on /tests/{subsystem}) --
    see tests/test_routes_tests.py for the detailed coverage of what
    those actions point at and who may reach them. What still holds here
    unconditionally: no <form> and no hx-get on /tests itself.
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
        the extraction spans the newlines between the body's heading
        and the closing </main> tag and yields non-empty, recognizable
        content, rather than merely being exercised indirectly through
        assertions that would also pass on "".
        """
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert body != "", "Extracted main body must not be empty"
        assert "Run all" in body
        assert "<h1" in body

    def test_no_form_in_body(self, client):
        """Body contains no <form> elements."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert "<form" not in body, "Tests level must contain no forms"

    def test_run_all_hx_post_in_body(self, client):
        """Body contains the Run-all button's hx-post -- Task 13a's real
        actions, unlike Task 11's placeholder this test used to guard
        the total absence of. The button POSTs to a route Task 13b adds;
        this only proves the markup points at it."""
        response = client.get("/tests")
        assert response.status_code == 200
        body = self._extract_main_body(response.text)
        assert 'hx-post="/tests/run-all"' in body

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
