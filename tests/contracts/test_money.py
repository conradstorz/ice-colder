from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from ice_contracts import money
from ice_contracts.money import (
    Cents,
    cents_to_dollar_text,
    dollars_to_cents,
    format_cents,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (2.675, 268),
        ("1.10", 110),
        (0.1 + 0.2, 30),
        (3, 300),
        (Decimal("0.005"), 0),
        (Decimal("0.015"), 2),
    ],
)
def test_dollars_to_cents(value, expected):
    assert dollars_to_cents(value) == expected


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), "abc"])
def test_dollars_to_cents_rejects(bad):
    with pytest.raises(ValueError):
        dollars_to_cents(bad)


@pytest.mark.parametrize(
    "cents,expected",
    [(125, "$1.25"), (0, "$0.00"), (-50, "-$0.50"), (100000, "$1000.00")],
)
def test_format_cents(cents, expected):
    assert format_cents(cents) == expected


def test_cents_to_dollar_text():
    assert cents_to_dollar_text(5) == "0.05"
    assert cents_to_dollar_text(125) == "1.25"


def test_cents_field_is_strict():
    class M(BaseModel):
        c: Cents

    assert M(c=5).c == 5
    for bad in (1.0, True, -1):
        with pytest.raises(ValidationError):
            M(c=bad)


def test_payment_reexports_money_types():
    from ice_contracts.board import payment

    assert payment.Cents is money.Cents
    assert payment.PositiveCents is money.PositiveCents


def test_dollars_to_cents_huge_value_is_value_error():
    with pytest.raises(ValueError):
        money.dollars_to_cents("1e30")
