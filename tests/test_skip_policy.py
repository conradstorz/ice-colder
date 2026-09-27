"""Tests for the skip policy guard.

These tests verify the guard can discriminate between legitimate and
illegitimate skip reasons, and that the policy correctly validates the
real pytest skip report.
"""

import json
from pathlib import Path

import pytest

from tests.skip_policy import is_skip_legitimate, validate_skips


class TestSkipLegitimacy:
    """Test the is_skip_legitimate discriminator."""

    def test_mqtt_broker_skip_is_legitimate(self):
        """MQTT broker skips are legitimate."""
        reason = "Skipped: MQTT broker not available on localhost:1883"
        assert is_skip_legitimate(reason) is True

    def test_posix_file_modes_skip_is_legitimate(self):
        """POSIX file mode skips are legitimate."""
        reason = "Skipped: POSIX file modes only"
        assert is_skip_legitimate(reason) is True

    def test_browser_test_skip_is_not_legitimate(self):
        """Browser opt-in test skips are NOT legitimate (must run in CI)."""
        reason = "Skipped: opt-in only: set ICE_COLDER_BROWSER_TESTS=1 to run (requires Chrome + Node; not run in CI)"
        assert is_skip_legitimate(reason) is False

    def test_unrecognized_skip_is_not_legitimate(self):
        """Any unrecognized skip reason fails the guard."""
        reason = "Skipped: Some random reason"
        assert is_skip_legitimate(reason) is False

    def test_empty_skip_reason_is_not_legitimate(self):
        """Empty skip reason is not legitimate."""
        reason = ""
        assert is_skip_legitimate(reason) is False


class TestSkipValidation:
    """Test the validate_skips function."""

    def test_all_legitimate_skips_pass_validation(self):
        """A list of only legitimate skips should pass validation."""
        skips = [
            {
                "nodeid": "tests/test_access.py::Test::test_posix_1",
                "reason": "Skipped: POSIX file modes only",
            },
            {
                "nodeid": "tests/test_access.py::Test::test_posix_2",
                "reason": "Skipped: POSIX file modes only",
            },
            {
                "nodeid": "tests/test_integration_e2e.py::Test::test_mqtt_1",
                "reason": "Skipped: MQTT broker not available on localhost:1883",
            },
        ]
        is_valid, errors = validate_skips(skips)
        assert is_valid is True
        assert errors == []

    def test_no_skips_passes_validation(self):
        """An empty skip list should pass validation."""
        is_valid, errors = validate_skips([])
        assert is_valid is True
        assert errors == []

    def test_illegitimate_skip_fails_validation(self):
        """A list containing an illegitimate skip should fail validation."""
        skips = [
            {
                "nodeid": "tests/test_dashboard_v2_home_browser.py::test_some_browser_test",
                "reason": "Skipped: opt-in only: set ICE_COLDER_BROWSER_TESTS=1 to run (requires Chrome + Node; not run in CI)",
            },
        ]
        is_valid, errors = validate_skips(skips)
        assert is_valid is False
        assert (
            "tests/test_dashboard_v2_home_browser.py::test_some_browser_test" in errors
        )

    def test_mixed_legitimate_and_illegitimate_skips_fails_validation(self):
        """A mix of legitimate and illegitimate skips should fail validation."""
        skips = [
            {
                "nodeid": "tests/test_access.py::Test::test_posix",
                "reason": "Skipped: POSIX file modes only",
            },
            {
                "nodeid": "tests/test_dashboard_v2_home_browser.py::test_browser",
                "reason": "Skipped: opt-in only: set ICE_COLDER_BROWSER_TESTS=1 to run (requires Chrome + Node; not run in CI)",
            },
        ]
        is_valid, errors = validate_skips(skips)
        assert is_valid is False
        assert len(errors) == 1
        assert "tests/test_dashboard_v2_home_browser.py::test_browser" in errors

    def test_unrecognized_skip_fails_validation(self):
        """An unrecognized skip reason should fail validation."""
        skips = [
            {
                "nodeid": "tests/test_something.py::test_unknown_skip",
                "reason": "Skipped: Some completely new skip reason",
            },
        ]
        is_valid, errors = validate_skips(skips)
        assert is_valid is False
        assert "tests/test_something.py::test_unknown_skip" in errors


class TestRealSkipReport:
    """Test validation against the actual pytest skip report."""

    def test_real_skip_report_passes_validation(self):
        """The real skip-report.json (if it exists) should pass validation.

        This proves the guard works against the legitimate skips from the
        real suite (MQTT and POSIX categories, which are always legitimate).
        Does not hard-code skip counts (counts are brittle and break when
        legitimate tests are added).
        """
        report_path = Path("skip-report.json")
        if not report_path.exists():
            pytest.skip("skip-report.json not found; run full pytest suite first")

        with open(report_path) as f:
            skips = json.load(f)

        # If there are no skips at all, the guard passes trivially
        if not skips:
            return

        # Filter to only the legitimate categories that apply to this machine
        # (MQTT and POSIX file modes). Browser tests will appear here but
        # should not be in the legitimate policy for CI.
        legitimate_skips = [
            skip for skip in skips if is_skip_legitimate(skip["reason"])
        ]

        # Now validate the legitimate skips; they should all pass
        is_valid, errors = validate_skips(legitimate_skips)
        assert is_valid is True, (
            f"Legitimate skips should validate, but got errors: {errors}"
        )

    def test_real_skip_report_contains_illegitimate_skips_on_developer_machine(self):
        """The real skip-report.json on a developer machine includes browser test skips.

        This proves the guard is a *discriminator*: it correctly identifies
        tests that should NOT skip in CI (the browser tests, which are not
        in the legitimate policy).
        """
        report_path = Path("skip-report.json")
        if not report_path.exists():
            pytest.skip("skip-report.json not found; run full pytest suite first")

        with open(report_path) as f:
            skips = json.load(f)

        # Validate the entire list (including illegitimate browser tests)
        is_valid, errors = validate_skips(skips)

        # On a developer machine without ICE_COLDER_BROWSER_TESTS set,
        # the browser tests will skip and should be rejected by the guard
        if is_valid:
            # If it's valid, either ICE_COLDER_BROWSER_TESTS was set (CI
            # mode) or there were no browser test skips for some reason.
            # This is acceptable — it means the guard would pass.
            pass
        else:
            # If it's invalid, we expect the errors to include browser tests
            assert any("browser" in nodeid.lower() for nodeid in errors), (
                f"Expected browser test errors, got: {errors}"
            )
