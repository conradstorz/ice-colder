import re

import pytest

from services.auth_policy import (
    MIN_PASSWORD_LENGTH,
    generate_admin_password,
    is_loopback,
    password_problem,
    pin_problem,
)


@pytest.mark.parametrize(
    "weak", ["changeme", "admin", "password", "ice-colder", "CHANGEME"]
)
def test_known_weak_passwords_rejected(weak):
    assert password_problem(weak) is not None


def test_short_password_rejected():
    assert password_problem("abcdefghijk") is not None  # 11 chars


def test_strong_password_accepted():
    assert password_problem("correct-horse-battery") is None


def test_empty_password_rejected():
    assert "empty" in password_problem("")


def test_generated_password_is_long_and_urlsafe():
    pw = generate_admin_password()
    assert len(pw) >= MIN_PASSWORD_LENGTH
    assert re.fullmatch(r"[A-Za-z0-9_-]+", pw)
    assert password_problem(pw) is None
    assert generate_admin_password() != pw


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


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
