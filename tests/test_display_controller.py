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
        dc.update_for_state("interacting_with_user")  # change away from advertising first
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
