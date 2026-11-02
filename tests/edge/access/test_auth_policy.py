import pytest

from ice_edge.access.auth_policy import pin_problem


@pytest.mark.parametrize("pin", ["1379", "9042", "13795", "90426183"])
def test_good_pins_accepted(pin):
    assert pin_problem(pin) is None


@pytest.mark.parametrize(
    "pin,fragment",
    [
        ("", "4"),
        ("123", "4"),
        ("123456789", "8"),
        ("12a4", "digits"),
        ("1111", "same digit"),
        ("1234", "run"),
        ("87654321", "run"),
        ("3456", "run"),
        ("4321", "run"),
    ],
)
def test_bad_pins_rejected(pin, fragment):
    problem = pin_problem(pin)
    assert problem is not None
    assert fragment in problem
