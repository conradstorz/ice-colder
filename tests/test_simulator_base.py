# tests/test_simulator_base.py
"""Tests for simulators/base.py — ESP32Simulator base class."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from config.config_model import ConfigModel
from contracts.vending_machine import SubsystemCapabilities
from services.build_info import BUILD_INFO
from simulators.base import (
    ESP32Simulator,
    FaultDef,
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
        assert caps.contract_version == "0.3.0"
        assert caps.hardware_id is not None

    def test_hardware_id_is_stable_and_distinct(self):
        a = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        b = ConcreteSimulator(subsystem_name="test", machine_id="vmc-t")
        c = ConcreteSimulator(subsystem_name="other", machine_id="vmc-t")
        assert a.fake_hardware_id() == b.fake_hardware_id()
        assert a.fake_hardware_id() != c.fake_hardware_id()
        assert a.fake_hardware_id().startswith("02:")
        assert len(a.fake_hardware_id()) == 17

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
