# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""
import asyncio
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
        assert args.broker == "localhost"
        assert args.port == 1883
        assert args.machine_id == "vmc-0000"

    def test_parse_custom(self):
        args = ESP32Simulator.parse_args(["--broker", "10.0.0.1", "--port", "1884", "--machine-id", "vmc-0042"])
        assert args.broker == "10.0.0.1"
        assert args.port == 1884
        assert args.machine_id == "vmc-0042"
