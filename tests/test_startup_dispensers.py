# tests/test_startup_dispensers.py
"""Tests for `services/startup_dispensers.py` -- startup wiring for
`dispensers.toml`. `load_dispenser_profiles(config)` loads and logs a
`DispenserProfiles` report without an event loop; a directory at the
dispensers path mirrors `load_config`'s own directory-exit behaviour.
`wire_dispenser_profiles(vmc, profiles)` hands the loaded profiles to both
the VMC and the routes module.
"""

from pathlib import Path

import pytest

import services.startup_dispensers as startup_dispensers
from config.config_model import ConfigModel
from services.dispensers import DispenserProfiles

COMPOSE_PATH = Path("docker-compose.yml")


def test_startup_loads_and_logs_report(tmp_path, monkeypatch, caplog):
    path = tmp_path / "dispensers.toml"
    path.write_text("schema_version = 2\n", encoding="utf-8")
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))
    caplog.set_level("WARNING")

    result = startup_dispensers.load_dispenser_profiles(ConfigModel())

    assert isinstance(result, DispenserProfiles)
    messages = [r.message for r in caplog.records]
    assert any("no [slot.N] tables found" in m for m in messages), messages


def test_startup_missing_file_warns_and_continues(tmp_path, monkeypatch, caplog):
    """No `dispensers.toml` at all (the fresh-clone/first-boot default,
    since the file is gitignored and only its .example is shipped) must
    log exactly one warning finding and return normally -- never exit."""
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(tmp_path / "dispensers.toml"))
    caplog.set_level("INFO")

    result = startup_dispensers.load_dispenser_profiles(ConfigModel())

    assert isinstance(result, DispenserProfiles)
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "not found" in warnings[0]


def test_startup_survives_load_exception(tmp_path, monkeypatch, caplog):
    """An unexpected exception out of `DispenserProfiles.load()` (not
    `IsADirectoryError`, which already exits cleanly) must never crash
    startup -- it is logged at error and the machine keeps running."""
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(tmp_path / "dispensers.toml"))
    caplog.set_level("ERROR")

    def raise_runtime_error(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(DispenserProfiles, "load", raise_runtime_error)

    result = startup_dispensers.load_dispenser_profiles(ConfigModel())

    assert isinstance(result, DispenserProfiles)
    errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("dispensers.toml could not be loaded" in m for m in errors)
    # A consumer must be able to tell "load blew up" from "loaded fine" by
    # looking at the report alone, not just the log.
    assert result.report.file_error
    assert len(result.report.errors) == 1
    assert "dispensers.toml could not be loaded" in result.report.errors[0].message
    assert "boom" in result.report.errors[0].message


def test_startup_exits_when_path_is_directory(tmp_path, monkeypatch):
    bogus = tmp_path / "dispensers.toml"
    bogus.mkdir()
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(bogus))

    with pytest.raises(SystemExit) as exc_info:
        startup_dispensers.load_dispenser_profiles(ConfigModel())
    assert exc_info.value.code == 1


def test_fixture_provides_two_profiles(dispenser_profiles):
    assert dispenser_profiles.profile_for_slot(1).mechanism == "bagged_ice"
    assert dispenser_profiles.profile_for_slot(2).mechanism == "water_fill"


def test_main_wires_profiles_into_vmc_and_routes(monkeypatch):
    """`main()` hands the loaded `DispenserProfiles` to both the `Machine`
    and the routes module, right after `machine.set_health_monitor(health)`
    -- via `wire_dispenser_profiles(machine, profiles)`, extracted out of
    `main()` since `main()` itself is an infinite event loop wrapped in
    `@logger.catch()` and cannot be exercised partially in a test.
    `main()`'s own behaviour is unchanged: this helper is just the same
    two calls `main()` makes, moved so they can be tested without an
    event loop. This test proves one load reaches both consumers with
    the SAME object.
    """
    from controller.machine import Machine
    from web_interface import routes

    machine_calls = []
    routes_calls = []
    monkeypatch.setattr(
        Machine, "set_dispenser_profiles", lambda self, p: machine_calls.append(p)
    )
    monkeypatch.setattr(routes, "set_dispenser_profiles", routes_calls.append)

    cfg = ConfigModel()
    machine = Machine(config=cfg)
    sentinel = DispenserProfiles(cfg)

    startup_dispensers.wire_dispenser_profiles(machine, sentinel)

    assert machine_calls == [sentinel]
    assert routes_calls == [sentinel]


def test_compose_sets_dispensers_env():
    """Only the `vmc` service wires ICE_COLDER_DISPENSERS -- the three
    simulators never read dispensers.toml (the VMC sends the whole slot
    profile in the dispense command, plan 2), so their copy of the env var
    was vestigial. Assert it appears exactly once, inside the vmc service
    block, rather than once per ICE_COLDER_CONFIG line."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    dispensers_count = text.count("ICE_COLDER_DISPENSERS=/app/data/dispensers.toml")
    assert dispensers_count == 1

    assert "\n  sim-ice-maker:" in text
    vmc_block = text.split("\n  sim-ice-maker:", 1)[0]
    assert vmc_block.count("ICE_COLDER_DISPENSERS=/app/data/dispensers.toml") == 1
