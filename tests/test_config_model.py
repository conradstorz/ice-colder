"""Tests for config/config_model.py — Pydantic configuration model."""
import json
import pytest
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
    assert len(cfg.products) >= 1
    assert isinstance(cfg.products[0], Product)


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
    p = Product(sku="ICE-001", name="Bag of Ice", price=2.50, track_inventory=True, inventory_count=50)
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
            "products": [
                {"sku": "T-001", "name": "Test Product", "price": 3.00}
            ],
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


def test_get_preferred_gateway_for_none():
    """Returns None when no matching gateway is configured."""
    cfg = ConfigModel()
    cfg.communication.snapchat_gateway = None
    person = Person(preferred_comm=[Channel.snapchat])
    result = cfg.get_preferred_gateway_for(person)
    assert result is None
