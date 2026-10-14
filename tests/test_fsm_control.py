"""Tests for services/fsm_control.py admin commands."""

from unittest.mock import create_autospec

from ice_common.config_model import ConfigModel
from controller.machine import Machine
from ice_edge.sale.vmc import VMC
from ice_edge.sale.fsm_control import perform_command


def test_reset_recovers_vmc_from_error():
    vmc = Machine(config=ConfigModel()).vmc
    vmc.error_occurred()
    assert vmc.state == "error"
    result = perform_command("reset", vmc)
    assert vmc.state == "idle"
    assert "Reset complete" in result


def test_reset_ignored_when_not_in_error():
    vmc = Machine(config=ConfigModel()).vmc
    result = perform_command("reset", vmc)
    assert vmc.state == "idle"
    assert "ignored" in result.lower()


def test_reset_without_vmc_reports_failure():
    assert "not available" in perform_command("reset", None).lower()


def test_unknown_command_still_reported():
    assert "Unknown" in perform_command("frobnicate", None)


def test_restart_returns_exact_message():
    """Restart command returns its expected message without VMC interaction."""
    mock_vmc = create_autospec(VMC, instance=True)
    result = perform_command("restart", mock_vmc)
    assert result == "Restart command sent"
    assert mock_vmc.mock_calls == []


def test_restart_with_none_vmc_returns_exact_message():
    """Restart command works with None VMC and doesn't interact with it."""
    result = perform_command("restart", None)
    assert result == "Restart command sent"


def test_shutdown_returns_exact_message():
    """Shutdown command returns its expected message without VMC interaction."""
    mock_vmc = create_autospec(VMC, instance=True)
    result = perform_command("shutdown", mock_vmc)
    assert result == "Shutdown command sent"
    assert mock_vmc.mock_calls == []


def test_shutdown_with_none_vmc_returns_exact_message():
    """Shutdown command works with None VMC and doesn't interact with it."""
    result = perform_command("shutdown", None)
    assert result == "Shutdown command sent"
