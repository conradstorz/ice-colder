# tests/test_inventory_manager.py
"""Tests for services/inventory_manager.py — persistent inventory tracking."""
import json
from pathlib import Path

import pytest
from config.config_model import Product
from services.inventory_manager import InventoryManager


@pytest.fixture
def tmp_inventory(tmp_path):
    """Return the path to a temporary inventory.json."""
    return tmp_path / "inventory.json"


def _products():
    return [
        Product(sku="ICE-SM", name="Small Ice", price=2.00, track_inventory=True, inventory_count=10),
        Product(sku="ICE-LG", name="Large Ice", price=3.50, track_inventory=True, inventory_count=5),
        Product(sku="WATER", name="Water", price=1.50, track_inventory=False, inventory_count=0),
    ]


class TestInitialization:
    def test_seeds_from_config(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.get_count("ICE-SM") == 10
        assert inv.get_count("ICE-LG") == 5
        assert inv.get_count("WATER") == 0

    def test_creates_file(self, tmp_inventory):
        InventoryManager(_products(), path=tmp_inventory)
        assert tmp_inventory.exists()
        data = json.loads(tmp_inventory.read_text())
        assert data["ICE-SM"] == 10

    def test_loads_from_existing_file(self, tmp_inventory):
        # Pre-populate with different counts
        tmp_inventory.write_text(json.dumps({"ICE-SM": 3, "ICE-LG": 1}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.get_count("ICE-SM") == 3
        assert inv.get_count("ICE-LG") == 1
        # New product not in file gets seeded from config
        assert inv.get_count("WATER") == 0

    def test_survives_corrupt_file(self, tmp_inventory):
        tmp_inventory.write_text("not json")
        inv = InventoryManager(_products(), path=tmp_inventory)
        # Falls back to config defaults
        assert inv.get_count("ICE-SM") == 10


class TestTracking:
    def test_is_tracked(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_tracked("ICE-SM") is True
        assert inv.is_tracked("WATER") is False

    def test_is_available_tracked_with_stock(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("ICE-SM") is True

    def test_is_available_tracked_no_stock(self, tmp_inventory):
        tmp_inventory.write_text(json.dumps({"ICE-SM": 0, "ICE-LG": 5, "WATER": 0}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("ICE-SM") is False

    def test_is_available_untracked_always_true(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("WATER") is True

    def test_unknown_sku_not_available(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        # Unknown SKU defaults to not tracked → available
        assert inv.is_available("UNKNOWN") is True


class TestDecrement:
    def test_decrement_reduces_count(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        assert inv.get_count("ICE-SM") == 9

    def test_decrement_persists_to_file(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        # Reload from file
        data = json.loads(tmp_inventory.read_text())
        assert data["ICE-SM"] == 9

    def test_decrement_does_not_go_negative(self, tmp_inventory):
        tmp_inventory.write_text(json.dumps({"ICE-SM": 0, "ICE-LG": 5, "WATER": 0}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        assert inv.get_count("ICE-SM") == 0


class TestSetAndAdd:
    def test_set_count(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.set_count("ICE-SM", 50)
        assert inv.get_count("ICE-SM") == 50

    def test_add_sku(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.add_sku("NEW-SKU", 20, tracked=True)
        assert inv.get_count("NEW-SKU") == 20
        assert inv.is_tracked("NEW-SKU") is True

    def test_get_all(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        counts = inv.get_all()
        assert counts == {"ICE-SM": 10, "ICE-LG": 5, "WATER": 0}
