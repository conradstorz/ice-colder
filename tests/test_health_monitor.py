# tests/test_health_monitor.py
"""Tests for health monitor, alert deduplication, and notifier."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

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
        assert summary["active_faults"] == []

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


async def test_run_survives_check_exception(monkeypatch):
    """A failing health check must not kill the run() loop."""
    monitor = HealthMonitor(check_interval=0.01)
    calls = {"n": 0}
    kept_going = asyncio.Event()

    async def exploding_check():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        kept_going.set()

    monkeypatch.setattr(monitor, "_check", exploding_check)
    task = asyncio.create_task(monitor.run())
    await asyncio.wait_for(kept_going.wait(), timeout=5.0)
    assert not task.done()  # loop survived the exception
    assert calls["n"] >= 2  # and kept checking afterwards
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestChannelsAndOffline:
    def test_record_channel_appears_in_summary(self):
        monitor = HealthMonitor()
        monitor.record_channel("compressor_current", 8.4)
        channels = monitor.get_summary()["channels"]
        assert channels["compressor_current"]["value"] == 8.4
        assert channels["compressor_current"]["age_seconds"] >= 0

    def test_mark_offline_makes_subsystem_stale(self):
        monitor = HealthMonitor()
        monitor.record_heartbeat("ice_maker")
        monitor.mark_offline("ice_maker")
        summary = monitor.get_summary()["subsystems"]["ice_maker"]
        assert summary["alive"] is False
        assert summary["stale"] is True

    def test_mark_offline_unknown_subsystem_is_harmless(self):
        HealthMonitor().mark_offline("nope")  # must not raise

    def test_mark_offline_unknown_subsystem_tracks_as_stale(self):
        """A subsystem that dies (LWT) before ever sending a live heartbeat
        must still be tracked so it shows up in the dashboard and can alert."""
        monitor = HealthMonitor()
        monitor.mark_offline("mdb")
        summary = monitor.get_summary()["subsystems"]
        assert "mdb" in summary
        assert summary["mdb"]["alive"] is False
        assert summary["mdb"]["stale"] is True

    @pytest.mark.asyncio
    async def test_mark_offline_unknown_subsystem_fires_stale_alert(self):
        """Previously-untracked-but-now-offline subsystem must trigger the
        stale alert on the next health check round."""
        monitor = HealthMonitor()
        monitor.update_mqtt_status(True)
        monitor.mark_offline("ice_maker")
        callback = AsyncMock()
        monitor.set_alert_callback(callback)

        await monitor._check()
        callback.assert_awaited_once()
        alert = callback.call_args[0][0]
        assert "ice_maker" in alert.message


class TestFaultPlumbing:
    async def test_raise_alert_carries_code_and_dedups(self):
        hm = HealthMonitor()
        received = []

        async def cb(alert):
            received.append(alert)

        hm.set_alert_callback(cb)
        await hm.raise_alert(
            "ICE-301:ICE-1",
            "error",
            "vmc",
            "fill timeout",
            code="ICE-301",
            product_sku="ICE-1",
        )
        await hm.raise_alert(
            "ICE-301:ICE-1",
            "error",
            "vmc",
            "fill timeout",
            code="ICE-301",
            product_sku="ICE-1",
        )
        assert len(received) == 1
        assert received[0].code == "ICE-301"
        assert received[0].product_sku == "ICE-1"

    async def test_clear_alert_rearms(self):
        hm = HealthMonitor()
        received = []

        async def cb(alert):
            received.append(alert)

        hm.set_alert_callback(cb)
        await hm.raise_alert("k", "warning", "vmc", "m")
        hm.clear_alert("k")
        await hm.raise_alert("k", "warning", "vmc", "m")
        assert len(received) == 2

    def test_set_active_faults_preserves_since(self, monkeypatch):
        import time as _time

        hm = HealthMonitor()
        fault = {
            "key": "ICE-1",
            "sku": "ICE-1",
            "product": "Ice",
            "code": "ICE-301",
            "severity": "lockout",
            "scope": "product",
            "description": "d",
        }
        t = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm.set_active_faults([fault])
        t[0] = 1030.0
        hm.set_active_faults([fault])
        summary = hm.get_summary()
        assert len(summary["active_faults"]) == 1
        assert summary["active_faults"][0]["code"] == "ICE-301"
        assert summary["active_faults"][0]["since_seconds"] == 30.0

    def test_set_active_faults_drops_cleared(self):
        hm = HealthMonitor()
        hm.set_active_faults(
            [
                {
                    "key": "a",
                    "sku": "a",
                    "product": "A",
                    "code": "ICE-301",
                    "severity": "lockout",
                    "scope": "product",
                    "description": "d",
                }
            ]
        )
        hm.set_active_faults([])
        assert hm.get_summary()["active_faults"] == []


def _configured_email_config() -> ConfigModel:
    """A config whose email gateway and owner address are not placeholders."""
    config = ConfigModel()
    config.communication.email_gateway.smtp_server = "smtp.mail.test"
    config.machine_owner.email = "owner@mail.test"
    return config


class TestNotifier:
    def test_notifier_creates(self):
        config = ConfigModel()
        notifier = Notifier(config)
        assert notifier._owner.name == config.machine_owner.name

    @pytest.mark.asyncio
    async def test_send_logs_alert(self):
        config = _configured_email_config()
        notifier = Notifier(config)
        alert = Alert(level="warning", source="test", message="test alert")

        # Should not raise even without real SMTP
        with patch.object(
            notifier, "_send_email", new_callable=AsyncMock
        ) as mock_email:
            await notifier.send(alert)
            mock_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_alert_is_not_suppressed_on_a_young_clock(self, monkeypatch):
        """loop.time() is monotonic-since-boot; a small value must not look 'recent'."""
        config = _configured_email_config()
        notifier = Notifier(config)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: 12.0)  # 12 s after boot
        with patch.object(
            notifier, "_send_email", new_callable=AsyncMock
        ) as mock_email:
            await notifier.send(Alert(level="warning", source="t", message="m"))
            mock_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_skips_placeholder_email_gateway(self):
        """A blank/default config points at smtp.example.com; never try to send."""
        config = ConfigModel()
        assert not config.communication.email_gateway.is_configured
        notifier = Notifier(config)
        alert = Alert(level="error", source="vmc", message="ICE-301 fill timeout")
        warnings: list[str] = []
        handle = logger.add(
            lambda m: warnings.append(str(m)), level="WARNING", format="{message}"
        )
        try:
            with patch.object(
                notifier, "_send_email", new_callable=AsyncMock
            ) as mock_email:
                await notifier.send(alert)
                notifier._last_sent.clear()  # bypass cooldown for the second send
                await notifier.send(alert)
                mock_email.assert_not_awaited()
        finally:
            logger.remove(handle)
        placeholder_warnings = [w for w in warnings if "not configured" in w]
        assert len(placeholder_warnings) == 1
        assert not any("Email send failed" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_send_skips_placeholder_owner_email(self):
        config = _configured_email_config()
        config.machine_owner.email = "user@example.com"
        notifier = Notifier(config)
        with patch.object(
            notifier, "_send_email", new_callable=AsyncMock
        ) as mock_email:
            await notifier.send(Alert(level="warning", source="t", message="m"))
            mock_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_cooldown(self):
        config = _configured_email_config()
        notifier = Notifier(config)
        notifier._cooldown_seconds = 9999  # long cooldown

        alert = Alert(level="warning", source="test", message="test alert")

        with patch.object(
            notifier, "_send_email", new_callable=AsyncMock
        ) as mock_email:
            await notifier.send(alert)
            await notifier.send(alert)  # should be suppressed
            assert mock_email.await_count == 1


class TestSubsystemIdentity:
    def _caps(self, **over):
        base = {
            "subsystem": "vending",
            "firmware": "abc1234",
            "contract_version": "0.2.0",
            "brand": "ice-colder",
            "model": "vending-sim",
            "hardware_id": "02:11:22:33:44:55",
            "ip": "172.18.0.5",
            "channels": [],
            "commands": ["dispense"],
        }
        base.update(over)
        return base

    def test_capabilities_before_heartbeat_is_never_seen(self):
        hm = HealthMonitor()
        hm.record_capabilities("vending", self._caps())
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["alive"] is False
        assert row["seconds_since_seen"] == float("inf")
        assert row["firmware"] == "abc1234"
        assert row["hardware_id"] == "02:11:22:33:44:55"
        assert row["uptime_seconds"] is None

    def test_heartbeat_gives_uptime_and_alive(self):
        hm = HealthMonitor()
        hm.record_heartbeat("vending", {"subsystem": "vending", "uptime_seconds": 321})
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["alive"] is True
        assert row["uptime_seconds"] == 321
        assert row["firmware"] is None
        assert row["commands"] == []
        assert row["channel_count"] == 0

    def test_lwt_uptime_is_none(self):
        hm = HealthMonitor()
        hm.record_heartbeat("mdb", {"subsystem": "mdb", "uptime_seconds": -1})
        assert hm.get_summary()["subsystems"]["mdb"]["uptime_seconds"] is None

    def test_capabilities_age_and_channel_count(self, monkeypatch):
        import time as _time

        hm = HealthMonitor()
        t = [500.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm.record_capabilities(
            "ice_maker",
            self._caps(
                subsystem="ice_maker",
                channels=[{"channel_id": "a"}, {"channel_id": "b"}],
            ),
        )
        t[0] = 545.0
        row = hm.get_summary()["subsystems"]["ice_maker"]
        assert row["channel_count"] == 2
        assert row["capabilities_age_seconds"] == 45.0

    def test_empty_row_shape(self):
        row = HealthMonitor.empty_subsystem_row()
        assert row["alive"] is False and row["stale"] is False
        assert row["firmware"] is None and row["commands"] == []

    def test_vmc_block(self, monkeypatch):
        import time as _time

        from services.build_info import BUILD_INFO

        t = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: t[0])
        hm = HealthMonitor(machine_id="vmc-0000")
        t[0] = 1060.0
        vmc = hm.get_summary()["vmc"]
        assert vmc["commit_short"] == BUILD_INFO.commit_short
        assert vmc["source"] == BUILD_INFO.source
        assert vmc["uptime_seconds"] == 60
        assert vmc["machine_id"] == "vmc-0000"
        assert vmc["python_version"].count(".") == 2
