# tests/test_display_controller.py
"""Tests for services/display_controller.py — display mode management."""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.display_controller import DisplayController
from services.mqtt_messages import DisplayMode


class TestDisplayControllerModes:
    def test_initial_mode_is_advertising(self):
        dc = DisplayController()
        assert dc.current_mode == DisplayMode.advertising

    def test_idle_maps_to_advertising(self):
        dc = DisplayController()
        dc.update_for_state("idle")
        assert dc.current_mode == DisplayMode.advertising

    def test_interacting_maps_to_transaction(self):
        dc = DisplayController()
        dc.update_for_state("interacting_with_user")
        assert dc.current_mode == DisplayMode.transaction

    def test_dispensing_maps_to_transaction(self):
        dc = DisplayController()
        dc.update_for_state("dispensing")
        assert dc.current_mode == DisplayMode.transaction

    def test_error_maps_to_error(self):
        dc = DisplayController()
        dc.update_for_state("error")
        assert dc.current_mode == DisplayMode.error

    def test_unknown_state_defaults_to_advertising(self):
        dc = DisplayController()
        dc.update_for_state(
            "interacting_with_user"
        )  # change away from advertising first
        dc.update_for_state("some_unknown_state")
        assert dc.current_mode == DisplayMode.advertising

    def test_no_change_skips_publish(self):
        dc = DisplayController()
        dc.update_for_state("idle")  # already advertising -> no change
        # No error, no publish (no mqtt attached)

    def test_set_mode_manual(self):
        dc = DisplayController()
        dc.set_mode(DisplayMode.maintenance)
        assert dc.current_mode == DisplayMode.maintenance


class TestDisplayControllerMQTT:
    def test_publish_without_mqtt_does_not_raise(self):
        dc = DisplayController()
        dc.update_for_state("error")  # should not raise

    @pytest.mark.asyncio
    async def test_publish_on_mode_change(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.update_for_state("error")  # advertising -> error, should publish
        # Give the task a chance to run
        await asyncio.sleep(0.01)

        mock_client.publish.assert_awaited_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == "cmd/display"

    @pytest.mark.asyncio
    async def test_no_publish_when_mode_unchanged(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.update_for_state("idle")  # already advertising, no change
        await asyncio.sleep(0.01)

        mock_client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_mode_always_publishes(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.set_mode(DisplayMode.advertising)  # same as current, but manual
        await asyncio.sleep(0.01)

        mock_client.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ordinary_state_change_carries_no_message(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.update_for_state("error")  # advertising -> error, should publish
        await asyncio.sleep(0.01)

        mock_client.publish.assert_awaited_once()
        command = mock_client.publish.call_args[0][1]
        assert command.message is None


class TestDisplayControllerSetupCode:
    @pytest.mark.asyncio
    async def test_show_setup_code_publishes_maintenance_with_grouped_digits(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)

        assert dc.current_mode == DisplayMode.maintenance
        assert dc.setup_code == "12345678"
        mock_client.publish.assert_awaited_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == "cmd/display"
        command = call_args[0][1]
        assert command.mode == DisplayMode.maintenance
        assert command.message == "Setup code: 1234 5678"

    @pytest.mark.asyncio
    async def test_state_change_while_code_held_publishes_nothing(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)
        mock_client.publish.reset_mock()

        dc.update_for_state("interacting_with_user")
        await asyncio.sleep(0.01)

        mock_client.publish.assert_not_awaited()
        assert dc.current_mode == DisplayMode.maintenance
        assert dc.setup_code == "12345678"

    @pytest.mark.asyncio
    async def test_clear_setup_code_returns_to_last_recorded_state(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)
        dc.update_for_state("interacting_with_user")  # recorded but not published
        await asyncio.sleep(0.01)
        mock_client.publish.reset_mock()

        dc.clear_setup_code()
        await asyncio.sleep(0.01)

        assert dc.setup_code is None
        assert dc.current_mode == DisplayMode.transaction
        mock_client.publish.assert_awaited_once()
        command = mock_client.publish.call_args[0][1]
        assert command.mode == DisplayMode.transaction
        assert command.message is None

    @pytest.mark.asyncio
    async def test_clear_setup_code_with_none_held_is_noop(self):
        dc = DisplayController()
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(mock_client, loop)

        dc.clear_setup_code()
        await asyncio.sleep(0.01)

        assert dc.setup_code is None
        mock_client.publish.assert_not_awaited()

    def test_show_setup_code_logs_at_warning_level(self, caplog):
        caplog.set_level("WARNING")
        dc = DisplayController()

        dc.show_setup_code("12345678")

        assert any(
            "1234 5678" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )


class _FakeMQTTClient:
    """Mimics MQTTClient.publish's "not connected" drop (services/mqtt_client.py
    lines 104-106) without a real broker: a publish attempted while
    `connected` is False is silently swallowed, exactly like the real class."""

    def __init__(self):
        self.connected = False
        self.published: list[tuple[str, object]] = []

    async def publish(self, topic_suffix, payload, qos=1, retain=False):
        if not self.connected:
            return
        self.published.append((topic_suffix, payload))


class TestDisplayControllerReconnect:
    """Copilot review, main.py:349: ensure_setup_mode() runs right after the
    MQTT client is constructed, before mqtt.run() connects it, so the setup
    code's publish is dropped — and because DisplayController.setup_code is
    already set at that point, nothing triggers a second publish attempt
    once the connection is actually up. republish() is the fix's hook."""

    @pytest.mark.asyncio
    async def test_setup_code_published_while_disconnected_is_dropped(self):
        dc = DisplayController()
        client = _FakeMQTTClient()  # starts disconnected, like a fresh MQTTClient
        loop = asyncio.get_running_loop()
        dc.set_mqtt(client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)

        assert client.published == []
        assert dc.setup_code == "12345678"  # held in memory regardless

    @pytest.mark.asyncio
    async def test_republish_after_connect_delivers_the_held_setup_code(self):
        dc = DisplayController()
        client = _FakeMQTTClient()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)
        assert client.published == []  # dropped, as above

        client.connected = True  # simulates the MQTT connection callback firing
        dc.republish()
        await asyncio.sleep(0.01)

        assert len(client.published) == 1
        topic, command = client.published[0]
        assert topic == "cmd/display"
        assert command.mode == DisplayMode.maintenance
        assert command.message == "Setup code: 1234 5678"

    @pytest.mark.asyncio
    async def test_republish_with_no_setup_code_resends_current_mode(self):
        dc = DisplayController()
        client = _FakeMQTTClient()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(client, loop)
        dc.update_for_state("error")  # advertising -> error, dropped (disconnected)
        await asyncio.sleep(0.01)
        assert client.published == []

        client.connected = True
        dc.republish()
        await asyncio.sleep(0.01)

        assert len(client.published) == 1
        command = client.published[0][1]
        assert command.mode == DisplayMode.error
        assert command.message is None

    @pytest.mark.asyncio
    async def test_republish_does_not_depend_on_setup_code_equality(self):
        """The bug: ensure_setup_mode()'s `setup_code != code` guard skips a
        republish once the in-memory value already matches, even though
        that value was never actually delivered. republish() must not use
        the same guard."""
        dc = DisplayController()
        client = _FakeMQTTClient()
        loop = asyncio.get_running_loop()
        dc.set_mqtt(client, loop)

        dc.show_setup_code("12345678")
        await asyncio.sleep(0.01)
        client.connected = True

        # A second call with the identical code still held — must still
        # publish, unlike show_setup_code's caller-side equality check.
        dc.republish()
        await asyncio.sleep(0.01)

        assert len(client.published) == 1
        assert client.published[0][1].message == "Setup code: 1234 5678"


class TestVMCDisplayIntegration:
    def test_vmc_accepts_display_controller(self):
        cfg = ConfigModel()
        vmc = VMC(config=cfg)
        dc = DisplayController()
        vmc.set_display_controller(dc)
        assert vmc._display_controller is dc

    def test_start_interaction_updates_display(self):
        cfg = ConfigModel()
        vmc = VMC(config=cfg)
        dc = DisplayController()
        vmc.set_display_controller(dc)

        vmc.start_interaction()
        assert dc.current_mode == DisplayMode.transaction

    def test_error_updates_display(self):
        cfg = ConfigModel()
        vmc = VMC(config=cfg)
        dc = DisplayController()
        vmc.set_display_controller(dc)

        vmc.error_occurred()
        assert dc.current_mode == DisplayMode.error

    def test_reset_from_error_returns_to_advertising(self):
        cfg = ConfigModel()
        vmc = VMC(config=cfg)
        dc = DisplayController()
        vmc.set_display_controller(dc)

        vmc.error_occurred()
        assert dc.current_mode == DisplayMode.error
        vmc.reset_state()
        assert dc.current_mode == DisplayMode.advertising
