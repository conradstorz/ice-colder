import pytest

from web_interface.filters import humanize_seconds


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
