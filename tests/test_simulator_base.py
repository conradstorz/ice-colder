# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from simulators.base import ESP32Simulator, FaultDef, RECOVERY_RANGES


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
        args = ESP32Simulator.parse_args(
            ["--broker", "10.0.0.1", "--port", "1884", "--machine-id", "vmc-0042"]
        )
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
        topic = (
            call_args[0][0]
            if call_args[0]
            else call_args.kwargs.get("topic", call_args[0][0])
        )
        assert topic == "homeassistant/sensor/vmc-0001_test_subsystem/fake_temp/config"
        payload_str = (
            call_args[0][1]
            if len(call_args[0]) > 1
            else call_args.kwargs.get("payload")
        )
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
        topic = (
            call_args[0][0]
            if call_args[0]
            else call_args.kwargs.get("topic", call_args[0][0])
        )
        assert "binary_sensor" in topic
        payload = json.loads(
            call_args[0][1]
            if len(call_args[0]) > 1
            else call_args.kwargs.get("payload")
        )
        assert payload["payload_on"] == "ON"
        assert payload["payload_off"] == "OFF"


class TestFaultRegistration:
    def test_register_fault_stores_def(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        assert len(sim._fault_defs) == 1
        assert sim._fault_defs[0].name == "test_fault"

    def test_register_fault_initialises_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        assert sim._fault_state["test_fault"] == {"active": False, "recover_at": 0.0}

    def test_active_fault_names_empty_initially(self):
        sim = ConcreteSimulator()
        assert sim._active_fault_names == set()

    def test_active_fault_names_reflects_active_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        assert "test_fault" in sim._active_fault_names

    def test_register_multiple_faults(self):
        sim = ConcreteSimulator()
        for name in ("fault_a", "fault_b", "fault_c"):
            sim.register_fault(
                FaultDef(
                    name=name,
                    category="short",
                    probability=0.1,
                    on_activate=AsyncMock(),
                    on_recover=AsyncMock(),
                    message=f"{name} message",
                )
            )
        assert len(sim._fault_defs) == 3
        assert set(sim._fault_state.keys()) == {"fault_a", "fault_b", "fault_c"}


class TestFaultLoop:
    @pytest.mark.asyncio
    async def test_activate_fault_sets_active_state(self):
        sim = ConcreteSimulator()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._activate_fault(client, fault)
        assert sim._fault_state["test_fault"]["active"] is True
        now = time.monotonic()
        lo, hi = RECOVERY_RANGES["short"]
        assert (
            now + lo - 1.0
            <= sim._fault_state["test_fault"]["recover_at"]
            <= now + hi + 1.0
        )

    @pytest.mark.asyncio
    async def test_activate_fault_calls_on_activate(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._activate_fault(client, fault)
        on_activate.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_try_roll_faults_activates_on_probability_1(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._try_roll_faults(client)
        assert sim._fault_state["test_fault"]["active"] is True
        on_activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_try_roll_faults_skips_on_probability_0(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._try_roll_faults(client)
        assert sim._fault_state["test_fault"]["active"] is False
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_roll_faults_only_one_at_a_time(self):
        sim = ConcreteSimulator()
        for name in ("fault_a", "fault_b"):
            sim.register_fault(
                FaultDef(
                    name=name,
                    category="short",
                    probability=1.0,
                    on_activate=AsyncMock(),
                    on_recover=AsyncMock(),
                    message=f"{name} message",
                )
            )
        client = AsyncMock()
        await sim._try_roll_faults(client)
        active_count = sum(1 for s in sim._fault_state.values() if s["active"])
        assert active_count == 1

    @pytest.mark.asyncio
    async def test_try_roll_faults_skips_if_fault_already_active(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        # Pre-activate the fault
        sim._fault_state["test_fault"]["active"] = True
        client = AsyncMock()
        await sim._try_roll_faults(client)
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_recoveries_clears_overdue_fault(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=on_recover,
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        sim._fault_state["test_fault"]["recover_at"] = (
            time.monotonic() - 1.0
        )  # past due
        client = AsyncMock()
        await sim._check_recoveries(client)
        assert sim._fault_state["test_fault"]["active"] is False
        on_recover.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_check_recoveries_leaves_non_overdue_fault(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=on_recover,
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() + 9999.0
        client = AsyncMock()
        await sim._check_recoveries(client)
        assert sim._fault_state["test_fault"]["active"] is True
        on_recover.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_alert_active_includes_recover_in(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault message",
            severity="warning",
        )
        client = AsyncMock()
        await sim._publish_alert(client, fault, "active", recover_in=300.0)
        client.publish.assert_called_once()
        topic, payload_str = client.publish.call_args[0]
        assert topic == "vmc/vmc-0001/alert/test_subsystem"
        payload = json.loads(payload_str)
        assert payload["subsystem"] == "test_subsystem"
        assert payload["fault"] == "test_fault"
        assert payload["status"] == "active"
        assert payload["message"] == "Test fault message"
        assert payload["severity"] == "warning"
        assert payload["recover_in_seconds"] == 300

    @pytest.mark.asyncio
    async def test_publish_alert_cleared_omits_recover_in(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=1.0,
            on_activate=AsyncMock(),
            on_recover=AsyncMock(),
            message="Test fault message",
        )
        client = AsyncMock()
        await sim._publish_alert(client, fault, "cleared")
        payload = json.loads(client.publish.call_args[0][1])
        assert "recover_in_seconds" not in payload
        assert payload["status"] == "cleared"


class TestFaultInject:
    @pytest.mark.asyncio
    async def test_handle_inject_activates_named_fault(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "test_fault"})
        assert sim._fault_state["test_fault"]["active"] is True
        on_activate.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_inject_ignores_unknown_fault(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        # Should not raise
        await sim._handle_inject_command(client, {"fault": "nonexistent_fault"})

    @pytest.mark.asyncio
    async def test_handle_inject_ignored_if_fault_already_active(self):
        sim = ConcreteSimulator()
        on_activate = AsyncMock()
        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=on_activate,
            on_recover=AsyncMock(),
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "test_fault"})
        on_activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_inject_ignored_if_different_fault_active(self):
        sim = ConcreteSimulator()
        on_activate_b = AsyncMock()
        for name, activate in [("fault_a", AsyncMock()), ("fault_b", on_activate_b)]:
            sim.register_fault(
                FaultDef(
                    name=name,
                    category="short",
                    probability=0.0,
                    on_activate=activate,
                    on_recover=AsyncMock(),
                    message=f"{name} message",
                )
            )
        # fault_a already active
        sim._fault_state["fault_a"]["active"] = True
        client = AsyncMock()
        await sim._handle_inject_command(client, {"fault": "fault_b"})
        on_activate_b.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_inject_ignores_missing_fault_key(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        # Empty payload — no "fault" key
        await sim._handle_inject_command(client, {})
        assert sim._active_fault_names == set()
