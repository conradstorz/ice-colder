# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from simulators.base import ESP32Simulator


class ConcreteSimulator(ESP32Simulator):
    """Minimal concrete subclass for testing the ABC."""
    def __init__(self, **kwargs):
        super().__init__(subsystem_name="test_subsystem", **kwargs)
        self.simulation_ran = False

    async def run_simulation(self, client):
        self.simulation_ran = True
        await asyncio.sleep(0.1)


class TestInit:
    def test_default_args(self):
        sim = ConcreteSimulator()
        assert sim.subsystem_name == "test_subsystem"
        assert sim.broker == "localhost"
        assert sim.port == 1883
        assert sim.machine_id == "vmc-0000"

    def test_custom_args(self):
        sim = ConcreteSimulator(broker="10.0.0.1", port=1884, machine_id="vmc-0042")
        assert sim.broker == "10.0.0.1"
        assert sim.port == 1884
        assert sim.machine_id == "vmc-0042"

    def test_topic_prefix(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        assert sim.topic_prefix == "vmc/vmc-0001"


class TestHeartbeat:
    def test_heartbeat_payload(self):
        sim = ConcreteSimulator()
        payload = sim._build_heartbeat()
        assert payload["subsystem"] == "test_subsystem"
        assert "uptime_seconds" in payload
        assert isinstance(payload["uptime_seconds"], int)


class TestCLIParsing:
    def test_parse_defaults(self):
        args = ESP32Simulator.parse_args([])
        assert args.config == "config.json"
        assert args.broker is None  # falls back to config
        assert args.port is None
        assert args.machine_id is None

    def test_parse_custom(self):
        args = ESP32Simulator.parse_args(["--broker", "10.0.0.1", "--port", "1884", "--machine-id", "vmc-0042"])
        assert args.broker == "10.0.0.1"
        assert args.port == 1884
        assert args.machine_id == "vmc-0042"


class TestHADiscovery:
    def test_default_returns_empty_list(self):
        sim = ConcreteSimulator()
        assert sim.ha_discovery_entities() == []

    def test_build_ha_device_block(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        device = sim._build_ha_device()
        assert device["identifiers"] == ["vmc-0001_test_subsystem"]
        assert "name" in device
        assert device["manufacturer"] == "ice-colder"
        assert device["via_device"] == "vmc-0001"

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_sends_retained_messages(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        sim.ha_discovery_entities = lambda: [
            {
                "component": "sensor",
                "object_id": "fake_temp",
                "name": "Fake Temperature",
                "state_topic_suffix": "sensors/temp/fake",
                "value_template": "{{ value_json.value }}",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
            },
        ]
        client = AsyncMock()
        await sim._publish_ha_discovery(client)

        client.publish.assert_called_once()
        call_args = client.publish.call_args
        topic = call_args[0][0] if call_args[0] else call_args.kwargs.get("topic", call_args[0][0])
        assert topic == "homeassistant/sensor/vmc-0001_test_subsystem/fake_temp/config"
        payload_str = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("payload")
        payload = json.loads(payload_str)
        assert payload["name"] == "Fake Temperature"
        assert payload["unique_id"] == "vmc-0001_test_subsystem_fake_temp"
        assert payload["state_topic"] == "vmc/vmc-0001/sensors/temp/fake"
        assert payload["device"]["identifiers"] == ["vmc-0001_test_subsystem"]
        assert call_args.kwargs.get("retain") is True

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_empty_entities_no_publish(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        await sim._publish_ha_discovery(client)
        client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_binary_sensor(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        sim.ha_discovery_entities = lambda: [
            {
                "component": "binary_sensor",
                "object_id": "fake_running",
                "name": "Fake Running",
                "state_topic_suffix": "events/fake",
                "value_template": "{{ 'ON' if value_json.event == 'on' else 'OFF' }}",
                "device_class": "running",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
        ]
        client = AsyncMock()
        await sim._publish_ha_discovery(client)

        call_args = client.publish.call_args
        topic = call_args[0][0] if call_args[0] else call_args.kwargs.get("topic", call_args[0][0])
        assert "binary_sensor" in topic
        payload = json.loads(call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("payload"))
        assert payload["payload_on"] == "ON"
        assert payload["payload_off"] == "OFF"
