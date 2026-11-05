"""Unit tests for the shared settings route helpers."""

import pytest

from ice_edge.web.routes.settings.common import split_lines


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("  \n , \n", []),
        ("a@x.com", ["a@x.com"]),
        ("a@x.com\nb@x.com", ["a@x.com", "b@x.com"]),
        ("a@x.com, b@x.com", ["a@x.com", "b@x.com"]),
        ("a\r\nb,\n\n c ,d", ["a", "b", "c", "d"]),
    ],
)
def test_split_lines(raw, expected):
    assert split_lines(raw) == expected
