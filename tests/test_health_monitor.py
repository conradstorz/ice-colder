# tests/test_health_monitor.py
"""Tests for health monitor, alert deduplication, and notifier."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.config_model import ConfigModel
from services.health_monitor import HealthMonitor, SubsystemStatus, Alert
from services.notifier import Notifier


# ── SubsystemStatus tests ────────────────────────────────────


class TestSubsystemStatus:
    def test_never_seen(self):
        s = SubsystemStatus(name="mdb")
        assert s.alive is False
        assert s.seconds_since_seen == float("inf")

    def test_seen(self):
        s = SubsystemStatus(name="mdb", last_seen=time.monotonic())
        assert s.alive is True
        assert s.seconds_since_seen < 1.0


# ── HealthMonitor recording tests ────────────────────────────


class TestHealthMonitorRecording:
    def test_record_heartbeat_new_subsystem(self):
        monitor = HealthMonitor()
        monitor.record_heartbeat("mdb", {"uptime_seconds": 100})
        summary = monitor.get_summary()
        assert "mdb" in summary["subsystems"]
        assert summary["subsystems"]["mdb"]["alive"] is True

    def test_record_heartbeat_updates_existing(self):
        monitor = HealthMonitor()
        monitor.record_heartbeat("mdb")
        monitor.record_heartbeat("mdb", {"uptime_seconds": 200})
        assert len(monitor._subsystems) == 1

    def test_record_temperature(self):
        monitor = HealthMonitor()
        monitor.record_temperature("evaporator", -15.0)
        summary = monitor.get_summary()
        assert "evaporator" in summary["temperatures"]
        assert summary["temperatures"]["evaporator"]["value"] == -15.0
        assert summary["temperatures"]["evaporator"]["in_range"] is True

    def test_temperature_out_of_range(self):
        monitor = HealthMonitor(temp_min=-20.0, temp_max=40.0)
        monitor.record_temperature("ambient", 55.0)
        summary = monitor.get_summary()
        assert summary["temperatures"]["ambient"]["in_range"] is False

    def test_update_mqtt_status(self):
        monitor = HealthMonitor()
        assert monitor.get_summary()["mqtt_connected"] is False
        monitor.update_mqtt_status(True)
        assert monitor.get_summary()["mqtt_connected"] is True

    def test_update_vmc_state(self):
        monitor = HealthMonitor()
        monitor.update_vmc_state("idle")
        assert monitor.get_summary()["vmc_state"] == "idle"


# ── HealthMonitor check logic tests ──────────────────────────


class TestHealthMonitorChecks:
    @pytest.mark.asyncio
    async def test_mqtt_disconnect_fires_alert(self):
        monitor = HealthMonitor()
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        callback.assert_awaited_once()
        alert = callback.call_args[0][0]
        assert alert.level == "warning"
        assert "MQTT" in alert.message

    @pytest.mark.asyncio
    async def test_mqtt_connected_no_alert(self):
        monitor = HealthMonitor()
        monitor.update_mqtt_status(True)
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        # No subsystems registered, MQTT connected, VMC not in error
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vmc_error_fires_alert(self):
        monitor = HealthMonitor()
        monitor.update_mqtt_status(True)
        monitor.update_vmc_state("error")
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        callback.assert_awaited_once()
        alert = callback.call_args[0][0]
        assert alert.level == "error"
        assert "error state" in alert.message

    @pytest.mark.asyncio
    async def test_stale_subsystem_fires_alert(self):
        monitor = HealthMonitor(subsystem_timeout=60.0)
        monitor.record_heartbeat("sensors")
        # Backdate the last_seen so it appears stale
        monitor._subsystems["sensors"].last_seen = time.monotonic() - 120.0
        monitor.update_mqtt_status(True)
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        callback.assert_awaited_once()
        alert = callback.call_args[0][0]
        assert "sensors" in alert.message

    @pytest.mark.asyncio
    async def test_temperature_out_of_range_fires_alert(self):
        monitor = HealthMonitor(temp_min=-20.0, temp_max=40.0)
        monitor.update_mqtt_status(True)
        monitor.record_temperature("bin_top", 60.0)
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        callback.assert_awaited_once()
        alert = callback.call_args[0][0]
        assert alert.level == "critical"
        assert "60.0" in alert.message

    @pytest.mark.asyncio
    async def test_alert_deduplication(self):
        """Same alert should only fire once until cleared."""
        monitor = HealthMonitor()
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()  # fires MQTT disconnect
        await monitor._check()  # should NOT fire again
        assert callback.await_count == 1

    @pytest.mark.asyncio
    async def test_alert_clears_on_recovery(self):
        """Alert should re-fire after the condition clears and recurs."""
        monitor = HealthMonitor()
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()  # fires MQTT disconnect
        assert callback.await_count == 1

        monitor.update_mqtt_status(True)  # recover
        monitor.update_mqtt_status(False)  # fail again

        await monitor._check()  # should fire again
        assert callback.await_count == 2


# ── get_summary tests ─────────────────────────────────────────


class TestGetSummary:
    def test_empty_summary(self):
        monitor = HealthMonitor(check_interval=30, subsystem_timeout=120)
        summary = monitor.get_summary()
        assert summary["mqtt_connected"] is False
        assert summary["vmc_state"] == "unknown"
        assert summary["subsystems"] == {}
        assert summary["temperatures"] == {}
        assert summary["check_interval"] == 30
        assert summary["subsystem_timeout"] == 120

    def test_full_summary(self):
        monitor = HealthMonitor()
        monitor.update_mqtt_status(True)
        monitor.update_vmc_state("idle")
        monitor.record_heartbeat("mdb")
        monitor.record_temperature("evaporator", -12.5)

        summary = monitor.get_summary()
        assert summary["mqtt_connected"] is True
        assert summary["vmc_state"] == "idle"
        assert "mdb" in summary["subsystems"]
        assert "evaporator" in summary["temperatures"]


# ── Notifier tests ────────────────────────────────────────────


class TestNotifier:
    def test_notifier_creates(self):
        config = ConfigModel()
        notifier = Notifier(config)
        assert notifier._owner.name == config.machine_owner.name

    @pytest.mark.asyncio
    async def test_send_logs_alert(self):
        config = ConfigModel()
        notifier = Notifier(config)
        alert = Alert(level="warning", source="test", message="test alert")

        # Should not raise even without real SMTP
        with patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email:
            await notifier.send(alert)
            mock_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_cooldown(self):
        config = ConfigModel()
        notifier = Notifier(config)
        notifier._cooldown_seconds = 9999  # long cooldown

        alert = Alert(level="warning", source="test", message="test alert")

        with patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email:
            await notifier.send(alert)
            await notifier.send(alert)  # should be suppressed
            assert mock_email.await_count == 1
