"""Tests for services/fsm_control.py admin commands."""

from config.config_model import ConfigModel
from controller.machine import Machine
from services.fsm_control import perform_command


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
