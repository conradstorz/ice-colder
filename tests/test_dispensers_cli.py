# tests/test_dispensers_cli.py
"""Tests for the dispensers CLI (`python -m services.dispensers`)."""

import json
import tempfile
from pathlib import Path

import pytest

from services.dispensers import main


GOOD = """\
# dispensers.toml — physical dispense parameters, one table per slot.
# Generated reference: dispensers.example.toml. Validate with
#   uv run python -m services.dispensers --check
schema_version = 1

[slot.1]
mechanism   = "bagged_ice"
product_sku = "ICE-10LB"          # must match a catalog product with kind = "ice"
                                  # whose slot is 1

[slot.1.agitate]
motor_channel      = "agitator_motor"
run_seconds        = 4.0          # 0.5–60
stall_current_amps = "unmonitored"   # a number here requires current_channel
current_channel    = "unmonitored"

[slot.1.fill]
motor_channel      = "auger_motor"
proof              = "bag_full_sensor"   # or "timed"
sensor_channel     = "bag_full_sensor"   # bag_full_sensor proof only
max_run_seconds    = 25.0         # 1–120; ICE-301 if the sensor never trips
stall_current_amps = "unmonitored"
current_channel    = "unmonitored"

[slot.1.release]
solenoid_channel      = "bag_drop_solenoid"
proof                 = "door_sensor"    # or "timed"
sensor_channel        = "door_sensor"    # door_sensor proof only
pulse_seconds         = 1.5       # 0.1–10
open_timeout_seconds  = 3.0       # door_sensor proof only; ICE-401 if never open
close_timeout_seconds = 5.0       # door_sensor proof only; ICE-402 if never closed

[slot.1.accessories.bag_fan]
channel      = "bag_fan"
on_during    = ["fill"]           # step names for this mechanism, or ["all"]
lead_seconds = 2.0                # 0–30, on this long before the step starts
lag_seconds  = 0.5                # 0–30, off this long after the step ends

[slot.1.accessories.vending_light]
channel      = "vending_now_light"
on_during    = ["all"]
lead_seconds = 0.0
lag_seconds  = 0.0

[slot.2]
mechanism   = "water_fill"
product_sku = "WATER-1GAL"

[slot.2.fill]
valve_channel          = "water_valve_solenoid"
proof                  = "flow_volume"   # or "timed"
flow_sensor_channel    = "water_flow_sensor"   # flow_volume proof only
target_volume_ml       = 3785      # flow_volume only; 50–50000
pulses_per_liter       = 450.0     # flow_volume only; > 0
min_flow_ml_per_second = 20.0      # flow_volume only; WTR-101 if below after grace
no_flow_grace_seconds  = 3.0       # flow_volume only; 0.5–30
over_dispense_percent  = 10.0      # flow_volume only; 0–50; WTR-102 if exceeded
max_fill_seconds       = 90.0      # 1–600; WTR-101 if volume not reached
"""

BAD = """\
schema_version = 1

[slot.1]
mechanism   = "bagged_ice"
product_sku = "ICE-10LB"

[slot.1.fill]
motor_channel      = "auger_motor"
proof              = "bag_full_sensor"
sensor_channel     = "bag_full_sensor"
max_run_seconds    = 500.0         # way out of range
stall_current_amps = "unmonitored"
current_channel    = "unmonitored"
"""


@pytest.fixture
def temp_config():
    """Create a temporary config.json with two products."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        config_data = {
            "version": "1.0.0",
            "machine_id": "vmc-test",
            "physical": {
                "common_name": "Test Machine",
                "serial_number": "0000-0000",
                "dispense_timeout_seconds": 120.0,
                "products": [
                    {"sku": "ICE-10LB", "slot": 1, "kind": "ice"},
                    {"sku": "WATER-1GAL", "slot": 2, "kind": "water"},
                ],
            },
        }
        config_path.write_text(json.dumps(config_data), encoding="utf-8")
        yield tmpdir, config_path


def test_check_ok_exit_0(temp_config, monkeypatch, capsys):
    """--check on a good file with temp config returns 0, output ends with counts."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(GOOD, encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check"])
    assert result == 0

    output = capsys.readouterr().out
    assert "0 error(s), 2 warning(s)" in output


def test_check_errors_exit_1(temp_config, monkeypatch, capsys):
    """--check on a file with errors returns 1, stdout contains error path."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(BAD, encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check"])
    assert result == 1

    output = capsys.readouterr().out
    # The character › is U+203A
    assert "Slot 1 › fill.max_run_seconds" in output


def test_check_with_capabilities_file_clears_warnings(temp_config, monkeypatch, capsys):
    """--capabilities FILE clears capability warnings."""

    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(GOOD, encoding="utf-8")

    # Create a capabilities file
    caps = {
        "subsystem": "vending",
        "firmware": "x",
        "contract_version": "0.8.0",
        "channels": [
            {
                "channel_id": "agitator_motor",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "auger_motor",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "bag_drop_solenoid",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "bag_fan",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "vending_now_light",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "water_valve_solenoid",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "output",
            },
            {
                "channel_id": "bag_full_sensor",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "input",
            },
            {
                "channel_id": "door_sensor",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "input",
            },
            {
                "channel_id": "water_flow_sensor",
                "kind": "binary",
                "interval_seconds": 1.0,
                "direction": "input",
            },
        ],
    }
    caps_path = Path(tmpdir) / "capabilities.json"
    caps_path.write_text(json.dumps(caps), encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check", "--capabilities", str(caps_path)])
    assert result == 0

    output = capsys.readouterr().out
    # With capabilities, warnings should be gone (output ends with "OK")
    assert output.rstrip().endswith("OK")


def test_example_prints_generator_output(capsys):
    """--example prints render_example() and returns 0."""
    result = main(["--example"])
    assert result == 0

    from services.dispensers_doc import render_example

    output = capsys.readouterr().out
    assert output == render_example()


def test_missing_file_is_warning_exit_0(temp_config, monkeypatch, capsys):
    """Missing dispensers.toml is a warning, returns 0."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check"])
    assert result == 0

    output = capsys.readouterr().out
    assert "not found" in output


def test_directory_exit_2(temp_config, monkeypatch, capsys):
    """If dispensers.toml is a directory, returns 2 and prints one line to stderr."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.mkdir()

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check"])
    assert result == 2

    _, err = capsys.readouterr()
    assert "directory" in err


def test_bad_config_exits_2_with_one_stderr_line(temp_config, monkeypatch, capsys):
    """Bad config JSON validation error exits 2 with exactly one line to stderr."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(GOOD, encoding="utf-8")

    # Write invalid config (missing required field)
    bad_cfg = Path(tmpdir) / "bad_config.json"
    bad_cfg.write_text('{"physical": "not_a_dict"}', encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check", "--config", str(bad_cfg)])
    assert result == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip().count("\n") == 0  # exactly one line (no newlines)
    assert "validation error" in err.lower() or "error reading config" in err.lower()


def test_bad_capabilities_file_exits_2(temp_config, monkeypatch, capsys):
    """Bad capabilities JSON validation error exits 2 with exactly one line to stderr."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(GOOD, encoding="utf-8")

    # Write invalid capabilities (missing required fields)
    bad_caps = Path(tmpdir) / "bad_capabilities.json"
    bad_caps.write_text('{"nope": 1}', encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    result = main(["--check", "--capabilities", str(bad_caps)])
    assert result == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip().count("\n") == 0  # exactly one line (no newlines)
    assert (
        "validation error" in err.lower() or "error reading capabilities" in err.lower()
    )


def test_check_is_default_when_no_flag_given(temp_config, monkeypatch, capsys):
    """When no --check or --example flag is given, --check is the default action."""
    tmpdir, config_path = temp_config
    dispensers_path = Path(tmpdir) / "dispensers.toml"
    dispensers_path.write_text(GOOD, encoding="utf-8")

    monkeypatch.setenv("ICE_COLDER_CONFIG", str(config_path))
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(dispensers_path))

    # Call with just the path and config, no --check flag
    result = main([str(dispensers_path), "--config", str(config_path)])
    assert result == 0

    output = capsys.readouterr().out
    # Output should end with the counts line (same as --check behavior)
    assert "0 error(s), 2 warning(s)" in output
