# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from simulators.vending_machine import VendingMachineSimulator


class TestInit:
    def test_creates_with_defaults(self):
        sim = VendingMachineSimulator()
        assert sim.subsystem_name == "vending"
        assert sim.num_buttons == 3

    def test_slot_types(self):
        sim = VendingMachineSimulator()
        assert sim.slot_type(0) == "ice"
        assert sim.slot_type(1) == "ice"
        assert sim.slot_type(2) == "water"


class TestDispenseSequence:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_correct_states(self):
        sim = VendingMachineSimulator()
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
        sim = VendingMachineSimulator()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=2)

        assert published_states[0] == "solenoid_open"
        assert published_states[-1] == "complete"


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = VendingMachineSimulator()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset({0, 1, 2})
        assert len(buttons) > 1  # should hit at least 2 of 3
