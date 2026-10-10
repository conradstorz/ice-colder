# tests/test_sale_context.py
"""Unit tests for `controller.sale_context.SaleContext` (VMC public surface
design, section 3, Task 4) -- construction defaults, `with_`'s
replace-not-mutate semantics, and frozen-ness. VMC-level behavior (how each
FSM callback replaces the in-flight sale) is covered in
tests/test_vmc_flows.py and tests/test_vmc_dispense_profiles.py, not here.
"""

import dataclasses

import pytest

from config.config_model import Product
from controller.sale_context import SaleContext

PRODUCT = Product(sku="ICE-1", name="Ice Bag", price=2.50, slot=0, kind="ice")
OTHER_PRODUCT = Product(sku="WATER-1", name="Water", price=1.00, slot=1, kind="water")


def test_construction_defaults():
    ctx = SaleContext(product=PRODUCT)
    assert ctx.product is PRODUCT
    assert ctx.shares is None
    assert ctx.is_test is False
    assert ctx.mechanism is None
    assert ctx.request_id is None
    assert ctx.seq == 0
    assert ctx.started_at == 0.0


def test_construction_with_all_fields():
    ctx = SaleContext(
        product=PRODUCT,
        shares={"cash_coin": 2.50},
        is_test=True,
        mechanism="bagged_ice",
        request_id="req-1",
        seq=3,
        started_at=12345.0,
    )
    assert ctx.product is PRODUCT
    assert ctx.shares == {"cash_coin": 2.50}
    assert ctx.is_test is True
    assert ctx.mechanism == "bagged_ice"
    assert ctx.request_id == "req-1"
    assert ctx.seq == 3
    assert ctx.started_at == 12345.0


def test_with_returns_a_new_instance_leaving_the_original_unchanged():
    original = SaleContext(product=PRODUCT, started_at=1.0)
    updated = original.with_(shares={"cash_coin": 2.50})

    assert updated is not original
    assert updated.shares == {"cash_coin": 2.50}
    # The original is untouched -- with_ never mutates in place.
    assert original.shares is None
    assert updated.product is original.product
    assert updated.started_at == original.started_at


def test_with_can_change_multiple_fields_at_once():
    original = SaleContext(product=PRODUCT)
    updated = original.with_(mechanism="bagged_ice", request_id="req-1", seq=5)

    assert updated.mechanism == "bagged_ice"
    assert updated.request_id == "req-1"
    assert updated.seq == 5
    # Fields not named in the call carry over unchanged.
    assert updated.product is original.product
    assert updated.is_test is original.is_test


def test_with_can_clear_a_field_back_to_none():
    original = SaleContext(product=PRODUCT, shares={"cash_coin": 2.50})
    cleared = original.with_(shares=None)

    assert cleared.shares is None
    assert original.shares == {"cash_coin": 2.50}  # original still unchanged


def test_with_can_replace_the_product_itself():
    original = SaleContext(product=PRODUCT)
    updated = original.with_(product=OTHER_PRODUCT)

    assert updated.product is OTHER_PRODUCT
    assert original.product is PRODUCT


def test_frozen_assignment_raises():
    ctx = SaleContext(product=PRODUCT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.product = OTHER_PRODUCT  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.is_test = True  # type: ignore[misc]
