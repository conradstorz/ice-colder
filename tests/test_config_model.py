"""Tests for config/config_model.py — Pydantic configuration model."""

import pytest
from pydantic import ValidationError

from config.config_model import (
    ConfigModel,
    Product,
    PhysicalDetails,
    PaymentConfig,
    CommunicationConfig,
    Person,
    Channel,
)


def test_default_config_model():
    """ConfigModel can be constructed with all defaults."""
    cfg = ConfigModel()
    assert cfg.version == "1.0.0"
    assert isinstance(cfg.physical, PhysicalDetails)
    assert isinstance(cfg.payment, PaymentConfig)
    assert isinstance(cfg.communication, CommunicationConfig)


def test_products_convenience_property():
    cfg = ConfigModel()
    assert cfg.products is cfg.physical.products
    assert cfg.products == []


def test_machine_owner_convenience_property():
    cfg = ConfigModel()
    owner = cfg.machine_owner
    assert isinstance(owner, Person)
    assert owner is cfg.physical.people.machine_owner


def test_product_defaults():
    p = Product()
    assert p.sku == "SAMPLE-SKU"
    assert p.price == 1.00
    assert p.track_inventory is False
    assert p.inventory_count == 0


def test_product_custom_values():
    p = Product(
        sku="ICE-001",
        name="Bag of Ice",
        price=2.50,
        track_inventory=True,
        inventory_count=50,
    )
    assert p.sku == "ICE-001"
    assert p.name == "Bag of Ice"
    assert p.price == 2.50
    assert p.track_inventory is True
    assert p.inventory_count == 50


def test_config_roundtrip_json():
    """ConfigModel can be serialized to JSON and deserialized back."""
    cfg = ConfigModel()
    json_str = cfg.model_dump_json()
    restored = ConfigModel.model_validate_json(json_str)
    assert restored.version == cfg.version
    assert len(restored.products) == len(cfg.products)


def test_config_from_dict():
    """ConfigModel can be created from a plain dict (as loaded from config.json)."""
    data = {
        "version": "2.0.0",
        "physical": {
            "common_name": "TestMachine",
            "serial_number": "1234-5678",
            "location": {"address": "456 Test Ave"},
            "people": {},
            "products": [{"sku": "T-001", "name": "Test Product", "price": 3.00}],
        },
    }
    cfg = ConfigModel.model_validate(data)
    assert cfg.version == "2.0.0"
    assert cfg.physical.common_name == "TestMachine"
    assert len(cfg.products) == 1
    assert cfg.products[0].name == "Test Product"


def test_get_preferred_gateway_for_email():
    cfg = ConfigModel()
    person = Person(preferred_comm=[Channel.email])
    result = cfg.get_preferred_gateway_for(person)
    assert result is not None
    channel, gateway = result
    assert channel == Channel.email


def test_web_config_defaults():
    cfg = ConfigModel()
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 26123


def test_web_config_has_no_admin_credential():
    """Authentication lives entirely in data/access.json now (Task 20) — no
    admin_username/admin_password field exists on WebConfig at all."""
    cfg = ConfigModel()
    assert not hasattr(cfg.web, "admin_username")
    assert not hasattr(cfg.web, "admin_password")


def test_get_preferred_gateway_for_none():
    """Returns None when no matching gateway is configured."""
    cfg = ConfigModel()
    cfg.communication.snapchat_gateway = None
    person = Person(preferred_comm=[Channel.snapchat])
    result = cfg.get_preferred_gateway_for(person)
    assert result is None


def test_product_default_slot_is_zero():
    """Product() with no args must keep working (used by routes.py/tests) —
    slot defaults to 0 rather than being required."""
    p = Product()
    assert p.slot == 0


def test_legacy_config_without_slot_assigns_list_index():
    """Old config.json files have no 'slot' key on products; loading one must
    assign slot = list index so dispensing keeps its old positional semantics."""
    data = {
        "physical": {
            "products": [
                {"sku": "A", "name": "Ice", "price": 3.00},
                {"sku": "B", "name": "Small Water", "price": 0.50},
                {"sku": "C", "name": "Large Water", "price": 2.00},
            ]
        }
    }
    cfg = ConfigModel.model_validate(data)
    assert [p.slot for p in cfg.products] == [0, 1, 2]


def test_config_with_explicit_slots_preserved():
    data = {
        "physical": {
            "products": [
                {"sku": "A", "name": "Ice", "price": 3.00, "slot": 5},
                {"sku": "B", "name": "Water", "price": 0.50, "slot": 2},
            ]
        }
    }
    cfg = ConfigModel.model_validate(data)
    assert [p.slot for p in cfg.products] == [5, 2]


def test_duplicate_slots_rejected():
    data = {
        "physical": {
            "products": [
                {"sku": "A", "name": "Ice", "price": 3.00, "slot": 0},
                {"sku": "B", "name": "Water", "price": 0.50, "slot": 0},
            ]
        }
    }
    with pytest.raises(ValidationError):
        ConfigModel.model_validate(data)


class TestDispenseTimeout:
    def test_default_is_120_seconds(self):
        from config.config_model import ConfigModel

        assert ConfigModel().physical.dispense_timeout_seconds == 120.0

    def test_rejects_below_10_seconds(self):
        import pytest
        from pydantic import ValidationError

        from config.config_model import ConfigModel

        with pytest.raises(ValidationError):
            ConfigModel.model_validate({"physical": {"dispense_timeout_seconds": 5}})

    def test_example_config_declares_it(self):
        import json
        from pathlib import Path

        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        assert raw["physical"]["dispense_timeout_seconds"] == 120


def test_product_kind_defaults_to_other_and_validates():
    from pydantic import ValidationError
    from config.config_model import Product

    assert Product().kind == "other"
    assert Product(kind="ice").kind == "ice"
    with pytest.raises(ValidationError):
        Product(kind="soda")
