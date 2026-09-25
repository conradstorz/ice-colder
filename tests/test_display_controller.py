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
