# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.config_model import ConfigModel
from simulators.vending_machine import VendingMachineSimulator, _classify_product


def _make_config() -> ConfigModel:
    """Build a 3-product config matching the real machine layout."""
    return ConfigModel.model_validate({
        "physical": {
            "products": [
                {"sku": "Ten Pounds Ice", "name": "Bagged Ice", "price": 3.00},
                {"sku": "One Gallon Water", "name": "Small Water", "price": 0.50},
                {"sku": "Five Gallons Water", "name": "Large Water", "price": 2.00},
            ]
        }
    })


def _make_sim(**kwargs) -> VendingMachineSimulator:
    return VendingMachineSimulator(config=_make_config(), **kwargs)


class TestClassifyProduct:
    def test_ice_product(self):
        assert _classify_product("Bagged Ice", "Ten Pounds Ice") == "ice"

    def test_water_by_name(self):
        assert _classify_product("Small Water", "WATER-1GAL") == "water"

    def test_water_by_sku(self):
        assert _classify_product("Jug Fill", "Five Gallons Water") == "water"

    def test_unknown_defaults_to_ice(self):
        assert _classify_product("Mystery", "UNKNOWN-SKU") == "ice"


class TestInit:
    def test_creates_with_config(self):
        sim = _make_sim()
        assert sim.subsystem_name == "vending"
        assert sim.num_buttons == 3

    def test_slot_types(self):
        sim = _make_sim()
        assert sim.slot_type(0) == "ice"
        assert sim.slot_type(1) == "water"
        assert sim.slot_type(2) == "water"

    def test_single_product_config(self):
        sim = VendingMachineSimulator()  # default ConfigModel has 1 product
        assert sim.num_buttons == 1


class TestDispenseSequence:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_correct_states(self):
        sim = _make_sim()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_ice_dispense(client, slot=0)

        assert published_states == ["motor_active", "fill_complete", "complete"]

    @pytest.mark.asyncio
    async def test_water_dispense_publishes_correct_states(self):
        sim = _make_sim()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=1)

        assert published_states[0] == "solenoid_open"
        assert published_states[-1] == "complete"


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = _make_sim()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset({0, 1, 2})
        assert len(buttons) > 1  # should hit at least 2 of 3
