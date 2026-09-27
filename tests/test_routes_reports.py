"""Tests for the Reports level (Task 9): GET /reports with activity table.

Tests cover: permission gates (view_reports for owner/secretary, 403 for tech/loader),
period parameter (24/168/720 valid, fallback to 24), no-recorder placeholder,
asyncio.to_thread offloading, and overflow-x: auto container for the table.

Fixtures come from tests/conftest.py.
"""

import re

import pytest
from services.access import Role
from web_interface import context

# The period tabs are rendered as
#   <a href="/reports?period={p}" class="...">{label}</a>
# with "border-blue-600" (and "text-blue-600") added to the class list only
# for the tab matching the current `period`. This regex pulls the class
# attribute for a given period's tab out of the rendered HTML so a test can
# tell *which* tab is active -- a signal that actually distinguishes one
# period from another, unlike a bare status-code check.
_TAB_CLASS_RE = 'href="/reports\\?period={p}"\\s+class="([^"]*)"'


def _active_period(html: str) -> int | None:
    """Return whichever of 24/168/720's tab carries the active styling,
    or None if no tab is marked active (e.g. the no-recorder placeholder,
    which renders no tabs at all)."""
    for p in (24, 168, 720):
        match = re.search(_TAB_CLASS_RE.format(p=p), html)
        assert match, f"tab for period={p} not found in rendered HTML"
        if "border-blue-600" in match.group(1):
            return p
    return None


class TestReportsPermissionGate:
    """GET /reports gates on view_reports: 200 for owner/secretary, 403 for tech/loader."""

    @pytest.mark.parametrize(
        "role,expected_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_reports_gate(self, login_as, role, expected_status):
        client = login_as(role)
        response = client.get("/reports")
        assert response.status_code == expected_status


class TestReportsPeriodParameter:
    """Period parameter accepts 24/168/720; invalid falls back to 24.

    These tests wire a real EventRecorder (as TestReportsRecorderOffloading
    and TestReportsTableMarkup already do) so the request reaches the period
    logic in reports.py instead of short-circuiting into the no-recorder
    placeholder branch, which never looks at `period` at all. The observable
    signal is which period's tab in the rendered nav carries the active
    ("border-blue-600") styling -- that's a signal that can actually tell
    period=24 apart from period=168, unlike a bare status-code check.
    """

    @pytest.fixture
    def wired_client(self, client, tmp_path):
        """The `client` fixture with a real EventRecorder wired in, so
        /reports renders the activity table (and reaches the period
        fallback logic) instead of the no-recorder placeholder."""
        from services.event_recorder import EventRecorder
        from web_interface import context as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            yield client
        finally:
            r.set_event_recorder(None)

    @pytest.mark.parametrize("period", [24, 168, 720])
    def test_valid_period(self, wired_client, period):
        """Each valid period value renders with *that* period's tab active."""
        response = wired_client.get(f"/reports?period={period}")
        assert response.status_code == 200
        assert _active_period(response.text) == period

    @pytest.mark.parametrize("bad_period", [999, -5, 0])
    def test_invalid_period_falls_back(self, wired_client, bad_period):
        """Invalid integer periods fall back to 24 without error."""
        response = wired_client.get(f"/reports?period={bad_period}")
        assert response.status_code == 200
        assert _active_period(response.text) == 24

    def test_non_integer_period_is_rejected(self, wired_client):
        """A non-integer `period` never reaches the fallback logic at all:
        FastAPI's `period: int` query-param coercion rejects it before the
        route body runs, so this is a 422 (FastAPI validation error), not a
        200 with a 24-hour fallback."""
        response = wired_client.get("/reports?period=abc")
        assert response.status_code == 422


class TestReportsNoRecorder:
    """With no recorder wired, /reports renders a neutral placeholder and returns 200."""

    def test_no_recorder_renders_placeholder(self, client):
        # Ensure no recorder is set
        context.set_event_recorder(None)
        response = client.get("/reports")
        assert response.status_code == 200
        assert "Activity data not available yet" in response.text


class TestReportsRecorderOffloading:
    """asyncio.to_thread offloads get_summary and get_historical_average calls."""

    def test_reports_offloads_to_thread(self, client, tmp_path, monkeypatch):
        from services.event_recorder import EventRecorder
        from web_interface import context as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)

        calls = []
        real_to_thread = r.asyncio.to_thread

        async def spying_to_thread(func, *args, **kwargs):
            calls.append((func, args))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(r.asyncio, "to_thread", spying_to_thread)
        try:
            resp = client.get("/reports?period=168")
            assert resp.status_code == 200
            assert (recorder.get_summary, (168,)) in calls
            assert (recorder.get_historical_average, (168,)) in calls
        finally:
            r.set_event_recorder(None)


class TestReportsTableMarkup:
    """The activity table is wrapped in overflow-x: auto container."""

    def test_table_has_overflow_x_container(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import context as r

        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            response = client.get("/reports")
            assert response.status_code == 200
            # Verify overflow-x: auto container is in the HTML
            assert "overflow-x-auto" in response.text
            # Verify there's a table element
            assert "<table" in response.text
        finally:
            r.set_event_recorder(None)


class TestReportsLevel:
    """Reports level renders inside the shell with proper structure."""

    def test_reports_extends_base_html(self, client):
        """Verify the page extends base.html by checking for shell elements."""
        response = client.get("/reports")
        assert response.status_code == 200
        # Check for base.html elements (header, main)
        assert "<header" in response.text
        assert "<main" in response.text
        # Check for breadcrumb/title
        assert "Reports" in response.text
