"""Tests for Settings > Reports (web_interface/routes/settings.py):
GET/POST /settings/reports, gated `edit_contacts` per spec §4.1 (not
`edit_secrets`) — the scheduled sales-summary email settings
(config.reports / config_model.ReportsConfig).

Fixture style copied from tests/test_routes_settings.py: `wired` seeds a
ConfigModel + AccessStore into web_interface.routes' module state,
`login_as(role)` signs a client in, `client` is an owner-authenticated
client, and the autouse `isolated_config_path` fixture (tests/conftest.py)
redirects config_store.CONFIG_PATH to `tmp_path / "config.json"`, so
`save_config` calls in this file write there rather than any real
config.json.
"""

import json

import pytest

from config.config_model import ConfigModel
from services.access import Role
from web_interface.routes import settings as settings_routes


class TestReportsPageGating:
    """GET /settings/reports: owner and secretary 200, tech and loader 403."""

    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/reports")
        assert resp.status_code == expect_status


class TestReportsTileVisibility:
    """The Reports sub-tile on /settings is gated the same as the page
    itself (edit_contacts): present for owner/secretary, absent for
    tech/loader."""

    def test_owner_sees_reports_tile(self, login_as, wired):
        owner = login_as(Role.owner)
        resp = owner.get("/settings")
        assert resp.status_code == 200
        assert "/settings/reports" in resp.text

    def test_secretary_sees_reports_tile(self, login_as, wired):
        secretary = login_as(Role.secretary)
        resp = secretary.get("/settings")
        assert resp.status_code == 200
        assert "/settings/reports" in resp.text

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_tech_and_loader_do_not_see_reports_tile(self, login_as, wired, role):
        """tech/loader lack edit_contacts and edit_secrets both, so
        /settings itself 403s for them (proven by TestReportsPageGating
        above and tests/test_routes_settings.py); this asserts the
        sub-tile's own gate would keep it out of the (403) response too,
        rather than merely repeating the whole-page 403."""
        worker = login_as(role)
        resp = worker.get("/settings")
        assert resp.status_code == 403
        assert "/settings/reports" not in resp.text


class TestReportsPage:
    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/reports",
            headers={"HX-Request": ""},
            data={
                "schedule": "daily",
                "hour": "8",
                "weekday": "0",
                "extra_recipients": "",
            },
        )
        assert resp.status_code == 403

    def test_get_renders_stored_values(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        cfg.reports.schedule = "weekly"
        cfg.reports.hour = 9
        cfg.reports.weekday = 4
        cfg.reports.extra_recipients = ["a@example.com", "b@example.com"]
        resp = client.get("/settings/reports")
        assert resp.status_code == 200
        # extra_recipients round-trips one address per line (this file's
        # chosen separator) — a comma-joined render here would silently
        # corrupt the field the moment it's saved again unchanged.
        assert "a@example.com\nb@example.com" in resp.text

    def test_post_round_trips_through_save_config(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "weekly",
                "hour": "14",
                "weekday": "3",
                "extra_recipients": "a@example.com\nb@example.com",
            },
        )
        assert resp.status_code == 200
        assert cfg.reports.schedule == "weekly"
        assert cfg.reports.hour == 14
        assert cfg.reports.weekday == 3
        assert cfg.reports.extra_recipients == ["a@example.com", "b@example.com"]

        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["reports"]["schedule"] == "weekly"
        assert saved["reports"]["hour"] == 14
        assert saved["reports"]["weekday"] == 3
        assert saved["reports"]["extra_recipients"] == [
            "a@example.com",
            "b@example.com",
        ]

    def test_comma_separated_extra_recipients_also_accepted(
        self, client, wired, tmp_path
    ):
        """Both separators are accepted on submit (matching the existing
        /settings/web trusted_proxies convention in this same module) even
        though the field always renders back one-per-line."""
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "daily",
                "hour": "7",
                "weekday": "0",
                "extra_recipients": "a@example.com, b@example.com",
            },
        )
        assert resp.status_code == 200
        assert cfg.reports.extra_recipients == ["a@example.com", "b@example.com"]

    @pytest.mark.parametrize("blank", ["", "   ", "\n\n  \n"])
    def test_blank_extra_recipients_round_trips_to_empty_list(
        self, client, wired, tmp_path, blank
    ):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "off",
                "hour": "7",
                "weekday": "0",
                "extra_recipients": blank,
            },
        )
        assert resp.status_code == 200
        assert cfg.reports.extra_recipients == []

        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["reports"]["extra_recipients"] == []

    def test_invalid_hour_rejected_with_reason_and_changes_nothing(
        self, client, wired, tmp_path
    ):
        cfg, _vmc, _inv, _store = wired
        # A valid POST first, so the negative case below is proven to be
        # about validation rejecting hour=24, not about the POST never
        # reaching the handler at all (e.g. a missing HX-Request header).
        valid = client.post(
            "/settings/reports",
            data={
                "schedule": "daily",
                "hour": "9",
                "weekday": "2",
                "extra_recipients": "",
            },
        )
        assert valid.status_code == 200
        assert cfg.reports.hour == 9

        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "daily",
                "hour": "24",
                "weekday": "2",
                "extra_recipients": "",
            },
        )
        assert resp.status_code == 200
        assert "Invalid report settings" in resp.text
        # Unchanged from the prior valid POST — not 24, and not silently
        # clamped to 23.
        assert cfg.reports.hour == 9
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["reports"]["hour"] == 9

    def test_invalid_weekday_rejected_with_reason_and_changes_nothing(
        self, client, wired, tmp_path
    ):
        cfg, _vmc, _inv, _store = wired
        valid = client.post(
            "/settings/reports",
            data={
                "schedule": "weekly",
                "hour": "10",
                "weekday": "5",
                "extra_recipients": "",
            },
        )
        assert valid.status_code == 200
        assert cfg.reports.weekday == 5

        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "weekly",
                "hour": "10",
                "weekday": "7",
                "extra_recipients": "",
            },
        )
        assert resp.status_code == 200
        assert "Invalid report settings" in resp.text
        assert cfg.reports.weekday == 5
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["reports"]["weekday"] == 5

    def test_save_config_failure_returns_form_with_error_and_does_not_revert(
        self, client, wired, monkeypatch
    ):
        """Spec §5 / part 2 pattern: the opposite of the rollback instinct
        — the in-memory model keeps the submitted values even though the
        write to disk failed."""
        cfg, _vmc, _inv, _store = wired

        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)

        resp = client.post(
            "/settings/reports",
            data={
                "schedule": "weekly",
                "hour": "11",
                "weekday": "5",
                "extra_recipients": "x@example.com",
            },
        )
        assert resp.status_code == 200
        assert "Could not save changes" in resp.text
        assert cfg.reports.schedule == "weekly"
        assert cfg.reports.hour == 11
        assert cfg.reports.weekday == 5
        assert cfg.reports.extra_recipients == ["x@example.com"]


def test_reports_config_defaults_are_not_secrets():
    """Confirms the brief's masking claim directly: none of ReportsConfig's
    fields is a SecretStr, so services/config_store.py's type-based
    masking has nothing to do here and needed no update."""
    cfg = ConfigModel()
    for value in (
        cfg.reports.schedule,
        cfg.reports.hour,
        cfg.reports.weekday,
        cfg.reports.extra_recipients,
    ):
        assert type(value).__name__ != "SecretStr"
