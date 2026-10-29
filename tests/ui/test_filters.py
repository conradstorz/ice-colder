import pytest

from ice_ui.filters import humanize_seconds


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "—"),
        (float("inf"), "—"),
        (0, "0s"),
        (45, "45s"),
        (59.9, "59s"),
        (60, "1m"),
        (754, "12m"),
        (3600, "1h 00m"),
        (10_980, "3h 03m"),
        (190_800, "2d 5h"),
    ],
)
def test_humanize_seconds(value, expected):
    assert humanize_seconds(value) == expected


@pytest.mark.parametrize(
    "state,expected",
    [
        ("idle", "Ready"),
        ("interacting_with_user", "Customer at machine"),
        ("dispensing", "Dispensing"),
        ("error", "Error"),
        ("something_new", "something new"),
        (None, "—"),
        ("", "—"),
    ],
)
def test_state_label(state, expected):
    from ice_ui.filters import state_label

    assert state_label(state) == expected


def test_friendly_time_accepts_iso_and_datetime():
    import datetime as dt

    from ice_ui.filters import friendly_time

    assert friendly_time("2026-10-28T14:05:33.123456+00:00") == "28 Oct 2026, 14:05"
    assert friendly_time(dt.datetime(2026, 1, 3, 9, 7)) == "3 Jan 2026, 09:07"


def test_friendly_time_passthrough_and_empty():
    from ice_ui.filters import friendly_time

    assert friendly_time("garbage") == "garbage"
    assert friendly_time(None) == ""
    assert friendly_time("") == ""
