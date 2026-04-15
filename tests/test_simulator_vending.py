# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""
import asyncio
from unittest.mock import AsyncMock

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

    def test_hardware_state_initialized(self):
        sim = _make_sim()
        assert sim._hw["auger_motor"] is False
        assert sim._hw["agitator_motor"] is False
        assert sim._hw["fan"] is False
        assert sim._hw["bag_full_sensor"] is False
        assert sim._hw["bag_drop_solenoid"] is False
        assert sim._hw["water_valve_solenoid"] is False
        assert sim._hw["water_flow_sensor"] is False
        assert sim._hw["bin_half_full"] is True
        assert sim._hw["heater_relay"] is False

    def test_cabinet_temp_initialized(self):
        sim = _make_sim()
        assert sim._cabinet_temp == 22.0

    def test_water_flow_starts_at_zero(self):
        sim = _make_sim()
        assert sim._water_flow_total == 0.0


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
    async def test_ice_dispense_hardware_sequence(self):
        """Verify hardware devices activate and deactivate in correct order."""
        sim = _make_sim()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if "hardware/io/" in topic:
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        await sim._run_ice_dispense(client, slot=0)

        # Agitator and fan should start first
        assert ("agitator_motor", True) in hw_events
        assert ("fan", True) in hw_events
        # Auger starts after
        assert ("auger_motor", True) in hw_events
        # Bag full triggers, auger stops
        assert ("bag_full_sensor", True) in hw_events
        assert ("auger_motor", False) in hw_events
        # Bag drops
        assert ("bag_drop_solenoid", True) in hw_events
        assert ("bag_drop_solenoid", False) in hw_events
        assert ("bag_full_sensor", False) in hw_events
        # Everything off at end
        assert ("agitator_motor", False) in hw_events
        assert ("fan", False) in hw_events

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

    @pytest.mark.asyncio
    async def test_water_dispense_hardware_sequence(self):
        """Verify water valve and flow sensor activate then deactivate."""
        sim = _make_sim()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if "hardware/io/" in topic:
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=1)

        assert ("water_valve_solenoid", True) in hw_events
        assert ("water_flow_sensor", True) in hw_events
        assert ("water_valve_solenoid", False) in hw_events
        assert ("water_flow_sensor", False) in hw_events

    @pytest.mark.asyncio
    async def test_water_dispense_increments_flow_total(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        assert sim._water_flow_total == 0.0
        await sim._run_water_dispense(client, slot=1)
        assert sim._water_flow_total > 0.0


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = _make_sim()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset({0, 1, 2})
        assert len(buttons) > 1  # should hit at least 2 of 3


class TestHADiscovery:
    def test_returns_12_entities(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 12

    def test_binary_sensor_count(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        binary = [e for e in entities if e["component"] == "binary_sensor"]
        assert len(binary) == 9

    def test_binary_sensor_names(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        binary_ids = {e["object_id"] for e in entities if e["component"] == "binary_sensor"}
        expected = {
            "auger_motor", "agitator_motor", "fan",
            "bag_full_sensor", "bag_drop_solenoid",
            "water_valve_solenoid", "water_flow_sensor",
            "bin_half_full", "heater_relay",
        }
        assert binary_ids == expected

    def test_cabinet_temp_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        temp = next(e for e in entities if e["object_id"] == "cabinet_temp")
        assert temp["component"] == "sensor"
        assert temp["device_class"] == "temperature"
        assert temp["unit_of_measurement"] == "\u00b0C"
        assert temp["state_topic_suffix"] == "sensors/temp/cabinet"

    def test_water_flow_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        flow = next(e for e in entities if e["object_id"] == "water_flow_total")
        assert flow["component"] == "sensor"
        assert flow["device_class"] == "water"
        assert flow["unit_of_measurement"] == "gal"
        assert flow["state_class"] == "total_increasing"

    def test_uptime_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "Vending Machine Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["state_topic_suffix"] == "heartbeat/vending"

    def test_all_object_ids_unique(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))
