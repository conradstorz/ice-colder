# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import aiomqtt
import pytest

from config.config_model import ConfigModel
from contracts.vending_machine import SubsystemCapabilities
from services.build_info import BUILD_INFO
from services.mqtt_client import PROTOCOL_VERSIONS
from contracts.common import SubsystemCommand
from simulators.base import (
    CommandOutcome,
    ESP32Simulator,
    FaultDef,
    IDEMPOTENCY_CACHE_SIZE,
    RECOVERY_RANGES,
    RECOVERY_RETRY_SECONDS,
)


class ConcreteSimulator(ESP32Simulator):
    """Minimal concrete subclass for testing the ABC."""

    def __init__(self, subsystem_name="test_subsystem", **kwargs):
        super().__init__(subsystem_name=subsystem_name, **kwargs)
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
        assert args.config is None  # None means: consult ICE_COLDER_CONFIG at load time
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
        assert sim._fault_state["test_fault"] == {
            "active": False,
            "recover_at": 0.0,
            "recovering": False,
        }

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
        # Recovery runs in a background task — wait for it to finish.
        for task in list(sim._recovery_tasks):
            await task
        assert sim._fault_state["test_fault"]["active"] is False
        on_recover.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_check_recoveries_marks_recovering_immediately(self):
        """The fault must stay 'active' while recovery is in flight."""
        sim = ConcreteSimulator()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_recover(client):
            started.set()
            await finish.wait()

        fault = FaultDef(
            name="slow_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=slow_recover,
            message="Slow fault",
        )
        sim.register_fault(fault)
        sim._fault_state["slow_fault"]["active"] = True
        sim._fault_state["slow_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        await asyncio.wait_for(sim._check_recoveries(client), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        assert sim._fault_state["slow_fault"]["active"] is True
        assert sim._fault_state["slow_fault"]["recovering"] is True
        assert "slow_fault" in sim._active_fault_names

        finish.set()
        for task in list(sim._recovery_tasks):
            await task
        assert sim._fault_state["slow_fault"]["active"] is False
        assert sim._fault_state["slow_fault"]["recovering"] is False

    @pytest.mark.asyncio
    async def test_check_recoveries_does_not_start_second_recovery(self):
        """A fault already recovering must not get a second recovery task."""
        sim = ConcreteSimulator()
        started = asyncio.Event()
        finish = asyncio.Event()
        call_count = 0

        async def slow_recover(client):
            nonlocal call_count
            call_count += 1
            started.set()
            await finish.wait()

        fault = FaultDef(
            name="slow_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=slow_recover,
            message="Slow fault",
        )
        sim.register_fault(fault)
        sim._fault_state["slow_fault"]["active"] = True
        sim._fault_state["slow_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        await asyncio.wait_for(sim._check_recoveries(client), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert len(sim._recovery_tasks) == 1

        # Second call while recovery is still in-flight must not add a task
        await asyncio.wait_for(sim._check_recoveries(client), timeout=1.0)
        assert len(sim._recovery_tasks) == 1
        assert call_count == 1

        finish.set()
        for task in list(sim._recovery_tasks):
            await task

    @pytest.mark.asyncio
    async def test_try_roll_faults_blocked_while_other_fault_recovering(self):
        """A slow recovery must not block a second fault from being excluded
        from rolling — recovering counts as active."""
        sim = ConcreteSimulator()
        finish = asyncio.Event()

        async def slow_recover(client):
            await finish.wait()

        recovering_fault = FaultDef(
            name="recovering_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=slow_recover,
            message="Recovering",
        )
        other_on_activate = AsyncMock()
        other_fault = FaultDef(
            name="other_fault",
            category="short",
            probability=1.0,
            on_activate=other_on_activate,
            on_recover=AsyncMock(),
            message="Other",
        )
        sim.register_fault(recovering_fault)
        sim.register_fault(other_fault)
        sim._fault_state["recovering_fault"]["active"] = True
        sim._fault_state["recovering_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        await sim._check_recoveries(client)
        await sim._try_roll_faults(client)
        other_on_activate.assert_not_called()

        finish.set()
        for task in list(sim._recovery_tasks):
            await task

    @pytest.mark.asyncio
    async def test_cancel_recovery_tasks_cancels_outstanding(self):
        sim = ConcreteSimulator()

        async def never_finishes(client):
            await asyncio.sleep(100)

        fault = FaultDef(
            name="stuck_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=never_finishes,
            message="Stuck",
        )
        sim.register_fault(fault)
        sim._fault_state["stuck_fault"]["active"] = True
        sim._fault_state["stuck_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()
        await sim._check_recoveries(client)
        assert len(sim._recovery_tasks) == 1

        sim._cancel_recovery_tasks()
        await asyncio.sleep(0)
        assert len(sim._recovery_tasks) == 0

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


class TestRecoveryFailure:
    @pytest.mark.asyncio
    async def test_run_recovery_failure_leaves_active_and_reschedules(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock(side_effect=RuntimeError("publish failed"))
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
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        await sim._check_recoveries(client)
        for task in list(sim._recovery_tasks):
            await task  # must not raise

        state = sim._fault_state["test_fault"]
        assert state["active"] is True
        assert state["recovering"] is False
        now = time.monotonic()
        assert now < state["recover_at"] <= now + RECOVERY_RETRY_SECONDS + 3.0

    @pytest.mark.asyncio
    async def test_check_recoveries_retries_after_failure(self):
        sim = ConcreteSimulator()
        on_recover = AsyncMock(side_effect=[RuntimeError("boom"), None])
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
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        # First recovery attempt fails.
        await sim._check_recoveries(client)
        for task in list(sim._recovery_tasks):
            await task
        assert sim._fault_state["test_fault"]["active"] is True
        assert sim._fault_state["test_fault"]["recovering"] is False

        # Simulate time passing: force the recover_at into the past again.
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() - 1.0

        # Second recovery attempt succeeds.
        await sim._check_recoveries(client)
        for task in list(sim._recovery_tasks):
            await task
        assert sim._fault_state["test_fault"]["active"] is False
        assert sim._fault_state["test_fault"]["recovering"] is False
        assert on_recover.call_count == 2

    @pytest.mark.asyncio
    async def test_run_recovery_cancelled_leaves_active_and_propagates(self):
        sim = ConcreteSimulator()
        started = asyncio.Event()

        async def cancellable_recover(client):
            started.set()
            await asyncio.sleep(100)

        fault = FaultDef(
            name="test_fault",
            category="short",
            probability=0.0,
            on_activate=AsyncMock(),
            on_recover=cancellable_recover,
            message="Test fault",
        )
        sim.register_fault(fault)
        sim._fault_state["test_fault"]["active"] = True
        sim._fault_state["test_fault"]["recover_at"] = time.monotonic() - 1.0
        client = AsyncMock()

        await sim._check_recoveries(client)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task = next(iter(sim._recovery_tasks))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = sim._fault_state["test_fault"]
        assert state["active"] is True
        assert state["recovering"] is False


class TestPublishAlertFormat:
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


class TestLoadConfig:
    def test_no_path_no_env_falls_back_to_default_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ICE_COLDER_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        config = ESP32Simulator.load_config()
        assert config.machine_id == "vmc-0000"

    def test_env_var_respected_when_no_path_given(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-config.json"
        custom.write_text(
            json.dumps({"machine_id": "vmc-env-test"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("ICE_COLDER_CONFIG", str(custom))
        config = ESP32Simulator.load_config()
        assert config.machine_id == "vmc-env-test"

    def test_explicit_path_overrides_env_var(self, tmp_path, monkeypatch):
        env_path = tmp_path / "env-config.json"
        env_path.write_text(json.dumps({"machine_id": "from-env"}), encoding="utf-8")
        explicit_path = tmp_path / "explicit-config.json"
        explicit_path.write_text(
            json.dumps({"machine_id": "from-explicit"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("ICE_COLDER_CONFIG", str(env_path))
        config = ESP32Simulator.load_config(str(explicit_path))
        assert config.machine_id == "from-explicit"

    def test_directory_at_path_falls_back_to_defaults(self, tmp_path):
        directory = tmp_path / "config.json"
        directory.mkdir()
        config = ESP32Simulator.load_config(str(directory))
        assert isinstance(config, ConfigModel)
        assert config.machine_id == "vmc-0000"

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        missing = tmp_path / "does-not-exist.json"
        config = ESP32Simulator.load_config(str(missing))
        assert isinstance(config, ConfigModel)

    def test_invalid_json_falls_back_to_defaults(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        config = ESP32Simulator.load_config(str(bad))
        assert isinstance(config, ConfigModel)

    def test_failed_validation_falls_back_to_defaults(self, tmp_path):
        bad = tmp_path / "invalid-schema.json"
        bad.write_text(
            json.dumps({"physical": {"products": "not-a-list"}}), encoding="utf-8"
        )
        config = ESP32Simulator.load_config(str(bad))
        assert isinstance(config, ConfigModel)

    def test_valid_config_loads_normally(self, tmp_path):
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"machine_id": "vmc-good"}), encoding="utf-8")
        config = ESP32Simulator.load_config(str(good))
        assert config.machine_id == "vmc-good"


class TestContractTransport:
    def test_build_will_targets_heartbeat_with_offline_marker(self):
        from simulators.ice_maker import IceMakerSimulator

        sim = IceMakerSimulator(machine_id="vmc-test")
        will = sim._build_will()
        assert will.topic == "vmc/vmc-test/heartbeat/ice_maker"
        assert json.loads(will.payload) == {
            "subsystem": "ice_maker",
            "uptime_seconds": -1,
        }
        assert will.qos == 1

    @pytest.mark.asyncio
    async def test_publish_passes_retain_flag(self):
        from unittest.mock import AsyncMock

        from simulators.ice_maker import IceMakerSimulator

        sim = IceMakerSimulator(machine_id="vmc-test")
        client = AsyncMock()
        await sim.publish(client, "capabilities/ice_maker", {"x": 1}, retain=True)
        assert client.publish.call_args.kwargs.get("retain") is True

    @pytest.mark.asyncio
    async def test_publish_defaults_to_qos_1(self):
        from unittest.mock import AsyncMock

        from simulators.ice_maker import IceMakerSimulator

        sim = IceMakerSimulator(machine_id="vmc-test")
        client = AsyncMock()
        await sim.publish(client, "ice_maker/event", {"x": 1})
        assert client.publish.call_args.kwargs.get("qos") == 1


class TestCapabilities:
    def test_default_capabilities_validate(self):
        sim = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        caps = sim.build_capabilities()
        assert isinstance(caps, SubsystemCapabilities)
        assert caps.subsystem == "test"
        assert caps.firmware == BUILD_INFO.commit_short
        assert caps.contract_version == "0.6.0"
        assert caps.hardware_id is not None

    def test_hardware_id_is_stable_and_distinct(self):
        a = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        b = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        c = ConcreteSimulator(subsystem_name="other", machine_id="vmc-t")
        assert a.fake_hardware_id() == b.fake_hardware_id()
        assert a.fake_hardware_id() != c.fake_hardware_id()
        assert a.fake_hardware_id().startswith("02:")
        assert len(a.fake_hardware_id()) == 17

    def test_standard_commands_always_advertised_even_with_none_of_its_own(self):
        """Copilot review (PR 22, id=4128088689): ping/self_test/
        force_report are registered on every subsystem in __init__
        (self._commands), independent of whatever a subclass's own
        SUPPORTED_COMMANDS lists -- ConcreteSimulator here declares no
        SUPPORTED_COMMANDS of its own at all, so an unfixed
        build_capabilities (commands=list(self.SUPPORTED_COMMANDS)) would
        advertise an empty list despite three real, registered handlers
        answering on the wire."""
        sim = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        caps = sim.build_capabilities()
        assert caps.commands == ["ping", "self_test", "force_report"]
        # And every advertised command is actually registered -- not just
        # a name in the list with no handler behind it.
        assert set(caps.commands) <= set(sim._commands)

    async def test_publish_capabilities_is_retained(self):
        sim = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        sim.publish = AsyncMock()
        await sim._publish_capabilities(None)
        sim.publish.assert_awaited_once()
        args, kwargs = sim.publish.await_args
        assert args[1] == "capabilities/test"
        assert isinstance(args[2], SubsystemCapabilities)
        assert kwargs.get("retain") is True


class TestCredentials:
    def test_credentials_from_config_unwrap_secret(self, monkeypatch):
        from pydantic import SecretStr

        for k in ("MQTT_USERNAME", "MQTT_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        cfg = ConfigModel()
        cfg.mqtt.username = "vmc"
        cfg.mqtt.password = SecretStr("pw-from-config")
        assert ESP32Simulator.credentials_from(cfg) == ("vmc", "pw-from-config")

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("MQTT_USERNAME", "envuser")
        monkeypatch.setenv("MQTT_PASSWORD", "envpw")
        cfg = ConfigModel()
        assert ESP32Simulator.credentials_from(cfg) == ("envuser", "envpw")

    def test_no_credentials_is_none_pair(self, monkeypatch):
        for k in ("MQTT_USERNAME", "MQTT_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        assert ESP32Simulator.credentials_from(ConfigModel()) == (None, None)

    async def test_run_passes_credentials_to_client(self, monkeypatch):
        import simulators.base as base

        captured = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)

            async def __aenter__(self):
                raise base.aiomqtt.MqttError("stop")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(base.aiomqtt, "Client", FakeClient)
        sim = ConcreteSimulator(username="u", password="p")
        task = asyncio.create_task(sim.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        assert captured["username"] == "u" and captured["password"] == "p"


class TestSimulatorProtocolVersion:
    """Simulators must negotiate the same MQTT version the VMC does."""

    @staticmethod
    async def _connect_kwargs(monkeypatch, cfg) -> dict:
        import simulators.base as base

        captured: dict = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)

            async def __aenter__(self):
                raise base.aiomqtt.MqttError("stop")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(base.aiomqtt, "Client", FakeClient)
        sim = ConcreteSimulator(config=cfg)
        task = asyncio.create_task(sim.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return captured

    async def test_defaults_to_v5(self, monkeypatch):
        captured = await self._connect_kwargs(monkeypatch, ConfigModel())
        assert captured["protocol"] is aiomqtt.ProtocolVersion.V5

    async def test_honors_configured_v311(self, monkeypatch):
        cfg = ConfigModel()
        cfg.mqtt.protocol_version = "3.1.1"
        captured = await self._connect_kwargs(monkeypatch, cfg)
        assert captured["protocol"] is aiomqtt.ProtocolVersion.V311

    async def test_matches_the_vmc_client_for_every_version(self, monkeypatch):
        """Parity: simulator and VMC resolve a version to the same enum."""
        for version, expected in PROTOCOL_VERSIONS.items():
            cfg = ConfigModel()
            cfg.mqtt.protocol_version = version
            captured = await self._connect_kwargs(monkeypatch, cfg)
            assert captured["protocol"] is expected


def _cmd(request_id: str, command: str, params: dict | None = None) -> SubsystemCommand:
    return SubsystemCommand(request_id=request_id, command=command, params=params or {})


class TestStandardCommands:
    @pytest.mark.asyncio
    async def test_ping_acks_ok(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-ping001", "ping"))
        client.publish.assert_called_once()
        topic, payload_str = client.publish.call_args[0]
        assert topic == "vmc/vmc-1/cmd/test_subsystem/ack"
        payload = json.loads(payload_str)
        assert payload["status"] == "ok"
        assert payload["result"] is None

    @pytest.mark.asyncio
    async def test_unknown_command_acks_unsupported(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-unk0001", "not_a_real_command"))
        payload = json.loads(client.publish.call_args[0][1])
        assert payload["status"] == "unsupported"


class TestSelfTest:
    def _register_two_faults(self, sim):
        for name in ("fault_a", "fault_b"):
            sim.register_fault(
                FaultDef(
                    name=name,
                    category="short",
                    probability=0.0,
                    on_activate=AsyncMock(),
                    on_recover=AsyncMock(),
                    message=f"{name} message",
                )
            )

    @pytest.mark.asyncio
    async def test_one_check_per_fault_none_active(self):
        sim = ConcreteSimulator()
        self._register_two_faults(sim)
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-st000001", "self_test"))
        payload = json.loads(client.publish.call_args[0][1])
        checks = payload["result"]["checks"]
        assert len(checks) == 2  # guard: the assertion below must not be vacuous
        assert all(c["pass"] for c in checks)

    @pytest.mark.asyncio
    async def test_fails_exactly_the_injected_fault(self):
        sim = ConcreteSimulator()
        self._register_two_faults(sim)
        sim._fault_state["fault_a"]["active"] = True
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-st000002", "self_test"))
        checks = json.loads(client.publish.call_args[0][1])["result"]["checks"]
        assert len(checks) == 2
        by_name = {c["name"]: c for c in checks}
        assert by_name["fault_a"]["pass"] is False
        assert by_name["fault_b"]["pass"] is True

    @pytest.mark.asyncio
    async def test_injecting_different_fault_changes_which_check_fails(self):
        sim = ConcreteSimulator()
        self._register_two_faults(sim)
        client = AsyncMock()

        sim._fault_state["fault_a"]["active"] = True
        await sim._handle_command(client, _cmd("req-st000003", "self_test"))
        first = {
            c["name"]: c["pass"]
            for c in json.loads(client.publish.call_args[0][1])["result"]["checks"]
        }
        assert first == {"fault_a": False, "fault_b": True}

        sim._fault_state["fault_a"]["active"] = False
        sim._fault_state["fault_b"]["active"] = True
        await sim._handle_command(client, _cmd("req-st000004", "self_test"))
        second = {
            c["name"]: c["pass"]
            for c in json.loads(client.publish.call_args[0][1])["result"]["checks"]
        }
        assert second == {"fault_a": True, "fault_b": False}
        assert first != second  # proves the check reflects *current* state


class TestForceReport:
    @pytest.mark.asyncio
    async def test_republishes_heartbeat_and_acks_ok(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-fr000001", "force_report"))
        topics = [call[0][0] for call in client.publish.call_args_list]
        assert "vmc/vmc-1/heartbeat/test_subsystem" in topics
        assert "vmc/vmc-1/cmd/test_subsystem/ack" in topics
        ack_payload = json.loads(client.publish.call_args_list[-1][0][1])
        assert ack_payload["status"] == "ok"

    @pytest.mark.asyncio
    async def test_calls_subclass_republish_hook(self):
        sim = ConcreteSimulator()
        sim._force_report_extra = AsyncMock()
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-fr000002", "force_report"))
        sim._force_report_extra.assert_awaited_once_with(client)


class TestIdempotencyCache:
    @pytest.mark.asyncio
    async def test_duplicate_request_id_runs_side_effect_once(self):
        sim = ConcreteSimulator()
        calls = []

        async def handler(client, cmd):
            calls.append(cmd.request_id)
            return {"n": len(calls)}

        sim.register_command("bump", handler)
        client = AsyncMock()

        cmd = _cmd("req-dup00001", "bump")
        await sim._handle_command(client, cmd)
        await sim._handle_command(client, cmd)
        await sim._handle_command(client, cmd)

        assert len(calls) == 1  # the observable side effect ran exactly once

    @pytest.mark.asyncio
    async def test_duplicate_request_id_republishes_identical_ack(self):
        sim = ConcreteSimulator()

        async def handler(client, cmd):
            return {"token": "first-and-only"}

        sim.register_command("bump", handler)
        client = AsyncMock()
        cmd = _cmd("req-dup00002", "bump")

        await sim._handle_command(client, cmd)
        first_payload = json.loads(client.publish.call_args[0][1])

        await sim._handle_command(client, cmd)
        second_payload = json.loads(client.publish.call_args[0][1])

        assert first_payload == second_payload

    @pytest.mark.asyncio
    async def test_cache_evicts_beyond_32_and_treats_old_id_as_new(self):
        sim = ConcreteSimulator()
        calls = []

        async def handler(client, cmd):
            calls.append(cmd.request_id)
            return None

        sim.register_command("bump", handler)
        client = AsyncMock()

        assert IDEMPOTENCY_CACHE_SIZE == 32
        request_ids = [f"req-evict{i:03d}" for i in range(IDEMPOTENCY_CACHE_SIZE + 1)]
        for rid in request_ids:
            await sim._handle_command(client, _cmd(rid, "bump"))

        assert len(calls) == IDEMPOTENCY_CACHE_SIZE + 1
        # The very first id was pushed out by the 33rd; it's no longer cached.
        oldest = request_ids[0]
        assert oldest not in sim._acked

        # Resending the evicted id must be treated as new: the handler runs
        # again (a second, observable call for that same request_id).
        await sim._handle_command(client, _cmd(oldest, "bump"))
        assert calls.count(oldest) == 2
        assert len(calls) == IDEMPOTENCY_CACHE_SIZE + 2

    @pytest.mark.asyncio
    async def test_still_in_window_id_is_not_treated_as_new(self):
        """Control for the eviction test: an id that hasn't fallen out of
        the last 32 must still be replayed from cache, not re-run."""
        sim = ConcreteSimulator()
        calls = []

        async def handler(client, cmd):
            calls.append(cmd.request_id)
            return None

        sim.register_command("bump", handler)
        client = AsyncMock()

        request_ids = [f"req-window{i:03d}" for i in range(IDEMPOTENCY_CACHE_SIZE)]
        for rid in request_ids:
            await sim._handle_command(client, _cmd(rid, "bump"))

        await sim._handle_command(client, _cmd(request_ids[0], "bump"))
        assert calls.count(request_ids[0]) == 1


class TestHandlerExceptions:
    @pytest.mark.asyncio
    async def test_raising_handler_acks_failed_with_detail(self):
        sim = ConcreteSimulator()

        async def handler(client, cmd):
            raise RuntimeError("motor jammed")

        sim.register_command("jam", handler)
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-jam00001", "jam"))

        payload = json.loads(client.publish.call_args[0][1])
        assert payload["status"] == "failed"
        assert payload["detail"] == "motor jammed"

    @pytest.mark.asyncio
    async def test_raising_handler_still_publishes_promptly_not_a_timeout(self):
        """A broken handler must ack immediately, never hang the loop."""
        sim = ConcreteSimulator()

        async def handler(client, cmd):
            raise ValueError("boom")

        sim.register_command("jam", handler)
        client = AsyncMock()
        await asyncio.wait_for(
            sim._handle_command(client, _cmd("req-jam00002", "jam")), timeout=1.0
        )
        client.publish.assert_called_once()


class TestCommandOutcome:
    @pytest.mark.asyncio
    async def test_handler_returning_command_outcome_controls_status(self):
        sim = ConcreteSimulator()

        async def handler(client, cmd):
            return CommandOutcome(status="rejected", detail="lockout")

        sim.register_command("locked", handler)
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-lock0001", "locked"))
        payload = json.loads(client.publish.call_args[0][1])
        assert payload["status"] == "rejected"
        assert payload["detail"] == "lockout"

    @pytest.mark.asyncio
    async def test_handler_returning_plain_dict_is_result_with_ok_status(self):
        sim = ConcreteSimulator()

        async def handler(client, cmd):
            return {"reading": 42}

        sim.register_command("read", handler)
        client = AsyncMock()
        await sim._handle_command(client, _cmd("req-read0001", "read"))
        payload = json.loads(client.publish.call_args[0][1])
        assert payload["status"] == "ok"
        assert payload["result"] == {"reading": 42}


class TestRegisterCommandHook:
    def test_register_command_does_not_require_editing_base(self):
        """The whole point of the hook: a subclass adds a command from its
        own __init__ with no change to simulators/base.py."""
        sim = ConcreteSimulator()
        assert "brew" not in sim._commands
        sim.register_command("brew", AsyncMock(return_value=None))
        assert "brew" in sim._commands

    def test_re_registering_a_name_replaces_the_handler(self):
        sim = ConcreteSimulator()
        first = AsyncMock(return_value=None)
        second = AsyncMock(return_value=None)
        sim.register_command("brew", first)
        sim.register_command("brew", second)
        assert sim._commands["brew"] is second

    @pytest.mark.asyncio
    async def test_registered_handler_receives_client_and_command(self):
        sim = ConcreteSimulator()
        received = {}

        async def handler(client, cmd):
            received["client"] = client
            received["cmd"] = cmd
            return None

        sim.register_command("brew", handler)
        client = AsyncMock()
        cmd = _cmd("req-brew0001", "brew", {"strength": "strong"})
        await sim._handle_command(client, cmd)
        assert received["client"] is client
        assert received["cmd"] is cmd


class TestCommandLoop:
    @pytest.mark.asyncio
    async def test_subscribes_to_cmd_subsystem_topic(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task = asyncio.create_task(sim._command_loop(client))
        await asyncio.sleep(0.02)
        client.subscribe.assert_any_call("vmc/vmc-1/cmd/test_subsystem")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_dispatches_a_queued_command_and_acks(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task = asyncio.create_task(sim._command_loop(client))
        await asyncio.sleep(0.02)
        topic, queue = next(
            (t, q) for t, q in sim._subscriptions if t == "vmc/vmc-1/cmd/test_subsystem"
        )
        queue.put_nowait(
            (topic, {"request_id": "req-loop0001", "command": "ping", "params": {}})
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        assert len(ack_calls) == 1
        assert json.loads(ack_calls[0][0][1])["status"] == "ok"

    @pytest.mark.asyncio
    async def test_invalid_payload_is_dropped_not_raised(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task = asyncio.create_task(sim._command_loop(client))
        await asyncio.sleep(0.02)
        topic, queue = next(
            (t, q) for t, q in sim._subscriptions if t == "vmc/vmc-1/cmd/test_subsystem"
        )
        queue.put_nowait((topic, {"not": "a valid command"}))
        await asyncio.sleep(0.02)
        assert not task.done()  # the loop is still alive, not crashed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.publish.assert_not_called()


class TestCommandLoopValidationRejection:
    """Defect 1 fix: a raw payload that fails `SubsystemCommand`'s param
    validator (out-of-range `water_valve`/`power_cycle` params) must not be
    silently dropped when it carries a usable `request_id` — the loop now
    acks it "rejected" itself, from inside `_command_loop`'s decode-and-
    validate, with no handler ever invoked. Uses `ConcreteSimulator`
    directly (not a real subsystem's handler): `COMMAND_PARAM_VALIDATORS`
    is enforced by `SubsystemCommand` itself, so this is genuinely shared,
    generic base-class behaviour, not something specific to any one
    simulator.
    """

    @staticmethod
    async def _start_loop(sim, client):
        task = asyncio.create_task(sim._command_loop(client))
        await asyncio.sleep(0.02)
        topic, queue = next(
            (t, q)
            for t, q in sim._subscriptions
            if t == f"{sim.topic_prefix}/cmd/{sim.subsystem_name}"
        )
        return task, topic, queue

    @staticmethod
    async def _stop_loop(task):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_out_of_range_water_valve_over_wire_acks_rejected(self):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task, topic, queue = await self._start_loop(sim, client)

        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-wv-out01",
                    "command": "water_valve",
                    "params": {"seconds": 11},
                },
            )
        )
        await asyncio.sleep(0.02)
        await self._stop_loop(task)

        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        assert len(ack_calls) == 1
        payload = json.loads(ack_calls[0][0][1])
        assert payload["request_id"] == "req-wv-out01"
        assert payload["command"] == "water_valve"
        assert payload["status"] == "rejected"
        assert "seconds" in payload["detail"]

    @pytest.mark.asyncio
    async def test_out_of_range_power_cycle_over_wire_acks_rejected(self):
        """Same fix, a different command — proves it is generic, not
        water_valve-specific."""
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task, topic, queue = await self._start_loop(sim, client)

        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-pc-out01",
                    "command": "power_cycle",
                    "params": {"dwell_seconds": 4},
                },
            )
        )
        await asyncio.sleep(0.02)
        await self._stop_loop(task)

        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        assert len(ack_calls) == 1
        payload = json.loads(ack_calls[0][0][1])
        assert payload["request_id"] == "req-pc-out01"
        assert payload["command"] == "power_cycle"
        assert payload["status"] == "rejected"
        assert "dwell_seconds" in payload["detail"]

    @pytest.mark.asyncio
    async def test_no_usable_request_id_is_dropped_without_an_ack(self):
        """Documents the deliberate limit: with nothing in the raw payload
        to correlate an ack to, the loop still just logs and drops."""
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task, topic, queue = await self._start_loop(sim, client)

        queue.put_nowait((topic, {"command": "water_valve", "params": {"seconds": 11}}))
        await asyncio.sleep(0.02)
        assert not task.done()  # still alive
        await self._stop_loop(task)
        client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_survives_bad_commands_and_still_serves_the_next_valid_one(
        self,
    ):
        sim = ConcreteSimulator(machine_id="vmc-1")
        client = AsyncMock()
        task, topic, queue = await self._start_loop(sim, client)

        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-wv-out02",
                    "command": "water_valve",
                    "params": {"seconds": 11},
                },
            )
        )
        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-pc-out02",
                    "command": "power_cycle",
                    "params": {"dwell_seconds": 4},
                },
            )
        )
        queue.put_nowait((topic, {"command": "water_valve", "params": {"seconds": 11}}))
        queue.put_nowait(
            (topic, {"request_id": "req-ping9999", "command": "ping", "params": {}})
        )
        await asyncio.sleep(0.05)
        await self._stop_loop(task)

        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        # Two rejections + the final ping; the request_id-less payload
        # produced no ack at all.
        assert len(ack_calls) == 3
        statuses = [json.loads(c[0][1])["status"] for c in ack_calls]
        assert statuses == ["rejected", "rejected", "ok"]
