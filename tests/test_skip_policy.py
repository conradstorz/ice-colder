"""Tests for the skip policy guard.

These tests verify the guard can discriminate between legitimate and
illegitimate skip reasons, that the pure report-building logic the
pytest_sessionfinish hook delegates to is correct, and that both the hook
and tests/run_skip_guard.py fail loudly -- never silently as "zero skips"
-- when they cannot see real skip data.

There used to be a third class here, TestRealSkipReport, which read
skip-report.json from the current working directory at test-collection
time. That file is written by tests/conftest.py's pytest_sessionfinish --
which runs *after* the whole session, this test class included, finishes --
so on every fresh checkout (every CI run) it could not exist yet, and both
tests unconditionally skipped with a reason matching no policy entry. That
made the guard fail every build and name its own tests as the offence, and
one of the two remaining assertions was tautological besides (it filtered
the input down to exactly what the function under test accepts, then
asserted the function accepted it -- true for any input, including empty
or corrupt data). It was deleted rather than patched: no variant of
"read the cwd's report during the run that writes it" is honest. The
coverage it pretended to give is replaced below by direct unit tests of
the extracted `build_skip_report` function and of the hook's two
failure-handling responsibilities, none of which depend on file-write
ordering.
"""

import json
from types import SimpleNamespace

from tests import conftest as skip_conftest
from tests.run_skip_guard import check_report
from tests.skip_policy import (
    MISSING_TERMINALREPORTER_MARKER,
    build_skip_report,
    is_error_marker,
    is_skip_legitimate,
    validate_skips,
)


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

    def test_malformed_skip_entry_fails_closed(self):
        """An entry that isn't even a dict must fail, not crash or vanish
        (a guard that raises on bad input is as unsafe as one that reports
        Guard OK on it: either way a real hole goes uninvestigated)."""
        is_valid, errors = validate_skips(["not-a-dict", 42])
        assert is_valid is False
        assert len(errors) == 2

    def test_non_string_reason_fails_closed_not_crash(self):
        """A `reason` key present with a non-string value -- a JSON null,
        surviving straight through .get("reason", "") since the key IS
        present, or any other non-string -- must not reach
        pattern.fullmatch and crash the guard. It has to fail closed like
        any other unrecognised reason instead."""
        skips = [
            {"nodeid": "t::null_reason", "reason": None},
            {"nodeid": "t::int_reason", "reason": 42},
            {"nodeid": "t::list_reason", "reason": ["Skipped: POSIX file modes only"]},
        ]
        is_valid, errors = validate_skips(skips)
        assert is_valid is False
        assert set(errors) == {"t::null_reason", "t::int_reason", "t::list_reason"}


class TestBuildSkipReport:
    """Unit tests for the pure report-building function the
    pytest_sessionfinish hook delegates to. These exercise the exact logic
    the hook runs, against constructed stand-in report objects -- no
    filesystem or pytest-session-ordering dependency, unlike the deleted
    TestRealSkipReport class (see module docstring)."""

    def test_extracts_nodeid_and_reason_from_tuple_longrepr(self):
        """pytest's normal shape: longrepr is (path, lineno, reason)."""
        stats = {
            "skipped": [
                SimpleNamespace(
                    nodeid="tests/test_a.py::test_one",
                    longrepr=("tests/test_a.py", 12, "Skipped: POSIX file modes only"),
                ),
                SimpleNamespace(
                    nodeid="tests/test_b.py::test_two",
                    longrepr=("tests/test_b.py", 34, "Skipped: something unrecognised"),
                ),
            ]
        }
        report = build_skip_report(stats)
        assert report == [
            {
                "nodeid": "tests/test_a.py::test_one",
                "reason": "Skipped: POSIX file modes only",
            },
            {
                "nodeid": "tests/test_b.py::test_two",
                "reason": "Skipped: something unrecognised",
            },
        ]

    def test_mixed_legitimate_and_illegitimate_reasons_feed_validate_skips(self):
        """The dicts build_skip_report produces are consumed unchanged by
        validate_skips -- proving the two functions actually compose."""
        stats = {
            "skipped": [
                SimpleNamespace(
                    nodeid="t::legit",
                    longrepr=(
                        "f",
                        1,
                        "Skipped: MQTT broker not available on localhost:1883",
                    ),
                ),
                SimpleNamespace(
                    nodeid="t::illegit",
                    longrepr=("f", 1, "Skipped: no policy entry for this"),
                ),
            ]
        }
        report = build_skip_report(stats)
        is_valid, errors = validate_skips(report)
        assert is_valid is False
        assert errors == ["t::illegit"]

    def test_empty_stats_mapping_produces_empty_report(self):
        """No 'skipped' key at all (nothing skipped this run) yields []."""
        assert build_skip_report({}) == []
        assert build_skip_report({"passed": [object()]}) == []

    def test_entry_with_no_extractable_reason_gets_empty_string_not_a_crash(self):
        """longrepr absent, None, or an unexpected shape must not raise --
        an unreadable reason still has to fail the guard (it matches no
        policy pattern), it just must not blow up report generation."""
        stats = {
            "skipped": [
                SimpleNamespace(nodeid="t::no_longrepr"),  # no .longrepr at all
                SimpleNamespace(nodeid="t::none_longrepr", longrepr=None),
                SimpleNamespace(nodeid="t::short_tuple", longrepr=("only", "two")),
            ]
        }
        report = build_skip_report(stats)
        assert report == [
            {"nodeid": "t::no_longrepr", "reason": ""},
            {"nodeid": "t::none_longrepr", "reason": ""},
            {"nodeid": "t::short_tuple", "reason": "('only', 'two')"},
        ]
        is_valid, errors = validate_skips(report)
        assert is_valid is False
        assert set(errors) == {"t::no_longrepr", "t::none_longrepr", "t::short_tuple"}


class TestErrorMarker:
    """The explicit marker the hook writes when it cannot see pytest's own
    stats, and is_error_marker's recognition of it (see
    TestGuardScriptRejectsBadReports below for the guard-side half of this
    proof)."""

    def test_marker_is_recognised_as_an_error(self):
        assert is_error_marker(MISSING_TERMINALREPORTER_MARKER) is True

    def test_a_normal_skip_list_is_not_mistaken_for_the_marker(self):
        assert is_error_marker([]) is False
        assert is_error_marker([{"nodeid": "t", "reason": "x"}]) is False

    def test_an_arbitrary_dict_without_the_marker_key_is_not_the_marker(self):
        assert is_error_marker({"nodeid": "t", "reason": "x"}) is False


class TestSessionFinishHook:
    """pytest_sessionfinish is a thin adapter over build_skip_report; these
    exercise its two failure-handling responsibilities directly, by calling
    it against a constructed stand-in session -- no waiting for a real
    pytest session to end."""

    @staticmethod
    def _fake_session(*, terminalreporter):
        pluginmanager = SimpleNamespace(get_plugin=lambda name: terminalreporter)
        config = SimpleNamespace(pluginmanager=pluginmanager)
        return SimpleNamespace(config=config)

    def test_writes_a_normal_report_when_terminalreporter_is_present(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        stats = {
            "skipped": [
                SimpleNamespace(
                    nodeid="t::a", longrepr=("f", 1, "Skipped: POSIX file modes only")
                ),
            ]
        }
        reporter = SimpleNamespace(stats=stats)
        skip_conftest.pytest_sessionfinish(
            self._fake_session(terminalreporter=reporter)
        )

        written = json.loads((tmp_path / "skip-report.json").read_text())
        assert written == [
            {"nodeid": "t::a", "reason": "Skipped: POSIX file modes only"}
        ]

    def test_writes_the_error_marker_when_terminalreporter_is_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        skip_conftest.pytest_sessionfinish(self._fake_session(terminalreporter=None))

        written = json.loads((tmp_path / "skip-report.json").read_text())
        assert is_error_marker(written)

    def test_writes_the_error_marker_when_terminalreporter_has_no_stats(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        reporter_without_stats = SimpleNamespace()  # no .stats attribute
        skip_conftest.pytest_sessionfinish(
            self._fake_session(terminalreporter=reporter_without_stats)
        )

        written = json.loads((tmp_path / "skip-report.json").read_text())
        assert is_error_marker(written)

    def test_a_write_failure_is_swallowed_not_raised(
        self, tmp_path, monkeypatch, capsys
    ):
        """The brief requires the local suite to keep passing even if the
        report file can't be written (locked, unwritable cwd, etc.) -- this
        proves the hook can never turn that into a local test failure."""
        monkeypatch.chdir(tmp_path)
        reporter = SimpleNamespace(stats={"skipped": []})

        real_open = open

        def _raise_on_report_write(file, mode="r", *a, **kw):
            if str(file).endswith("skip-report.json") and "w" in mode:
                raise OSError("simulated: file locked")
            return real_open(file, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", _raise_on_report_write)

        # Must not raise.
        skip_conftest.pytest_sessionfinish(
            self._fake_session(terminalreporter=reporter)
        )

        assert "WARNING" in capsys.readouterr().err
        assert not (tmp_path / "skip-report.json").exists()


class TestGuardScriptRejectsBadReports:
    """tests/run_skip_guard.py's check_report must fail loudly on every way
    the report can go wrong, never degrading to Guard OK."""

    def test_missing_file_fails(self, tmp_path, capsys):
        exit_code = check_report(tmp_path / "does-not-exist.json")
        assert exit_code == 1
        assert "not found" in capsys.readouterr().err

    def test_unparseable_json_fails(self, tmp_path, capsys):
        path = tmp_path / "skip-report.json"
        path.write_text("{not valid json")
        exit_code = check_report(path)
        assert exit_code == 1
        assert "could not parse" in capsys.readouterr().err.lower()

    def test_missing_terminalreporter_marker_fails_loudly(self, tmp_path, capsys):
        path = tmp_path / "skip-report.json"
        path.write_text(json.dumps(MISSING_TERMINALREPORTER_MARKER))
        exit_code = check_report(path)
        out, err = capsys.readouterr()
        assert exit_code == 1
        # Deliberately not matching on "terminalreporter": pytest's own
        # tmp_path fixture derives its directory name from this test's own
        # name ("test_missing_terminalreporter_..."), so that substring
        # would appear in `err` (via the printed report path) even if the
        # guard fell back to its generic "not a valid skip report" message
        # instead of actually recognising the marker -- a false pass hiding
        # in the very report meant to prove the opposite. Match the
        # marker-specific phrase from its `detail` text instead.
        assert "could not be collected" in err.lower()
        assert "Guard OK" not in out

    def test_a_non_list_non_marker_report_fails(self, tmp_path, capsys):
        path = tmp_path / "skip-report.json"
        path.write_text(json.dumps({"unexpected": "shape"}))
        exit_code = check_report(path)
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert "not a valid skip report" in err
        assert "Guard OK" not in out

    def test_a_legitimate_report_passes(self, tmp_path, capsys):
        path = tmp_path / "skip-report.json"
        path.write_text(
            json.dumps([{"nodeid": "t::a", "reason": "Skipped: POSIX file modes only"}])
        )
        exit_code = check_report(path)
        assert exit_code == 0
        assert "Guard OK" in capsys.readouterr().out

    def test_an_illegitimate_report_fails_naming_the_nodeid(self, tmp_path, capsys):
        path = tmp_path / "skip-report.json"
        path.write_text(
            json.dumps([{"nodeid": "t::bad", "reason": "Skipped: no policy for this"}])
        )
        exit_code = check_report(path)
        err = capsys.readouterr().err
        assert exit_code == 1
        assert "t::bad" in err

    def test_a_null_reason_fails_through_the_guards_own_error_path(
        self, tmp_path, capsys
    ):
        """A report entry with `"reason": null` (valid JSON, e.g. from a
        malformed upstream report) must produce the guard's own
        `::error::Guard FAILED` message naming the nodeid -- not an
        uncaught TypeError/traceback from pattern.fullmatch(None)."""
        path = tmp_path / "skip-report.json"
        path.write_text(json.dumps([{"nodeid": "t::null_reason", "reason": None}]))
        exit_code = check_report(path)
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert "::error::Guard FAILED" in err
        assert "t::null_reason" in err
        assert "Guard OK" not in out
