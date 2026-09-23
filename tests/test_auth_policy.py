import re

import pytest

from services.auth_policy import (
    MIN_PASSWORD_LENGTH,
    generate_admin_password,
    is_loopback,
    password_problem,
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
