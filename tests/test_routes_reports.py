"""Tests for the Reports level (Task 9): GET /reports with activity table.

Tests cover: permission gates (view_reports for owner/secretary, 403 for tech/loader),
period parameter (24/168/720 valid, fallback to 24), no-recorder placeholder,
asyncio.to_thread offloading, and overflow-x: auto container for the table.

Fixtures come from tests/conftest.py.
"""

import pytest
from services.access import Role
from web_interface import context


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
    """Period parameter accepts 24/168/720; invalid falls back to 24."""

    @pytest.mark.parametrize("period", [24, 168, 720])
    def test_valid_period(self, client, period):
        response = client.get(f"/reports?period={period}")
        assert response.status_code == 200

    def test_invalid_period_falls_back(self, client):
        """Invalid period (999) should fall back to 24 without error."""
        response = client.get("/reports?period=999")
        assert response.status_code == 200


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
