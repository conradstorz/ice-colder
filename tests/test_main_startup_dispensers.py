# tests/test_main_startup_dispensers.py
"""Tests for Task 7 (plan: dispenser profiles) -- startup wiring for
`dispensers.toml`. `main.load_dispenser_profiles(config)` is extracted out
of `main()` so it can load and log a `DispenserProfiles` report without an
event loop; a directory at the dispensers path mirrors `load_config`'s own
directory-exit behaviour. This task changes no runtime behaviour: the
report is only loaded and logged, never raised as a fault (plan 2).
"""

from pathlib import Path

import pytest

import main as main_mod
from config.config_model import ConfigModel
from services.dispensers import DispenserProfiles

COMPOSE_PATH = Path("docker-compose.yml")


def test_startup_loads_and_logs_report(tmp_path, monkeypatch, caplog):
    path = tmp_path / "dispensers.toml"
    path.write_text("schema_version = 2\n", encoding="utf-8")
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))
    caplog.set_level("WARNING")

    result = main_mod.load_dispenser_profiles(ConfigModel())

    assert isinstance(result, DispenserProfiles)
    messages = [r.message for r in caplog.records]
    assert any("no [slot.N] tables found" in m for m in messages), messages


def test_startup_exits_when_path_is_directory(tmp_path, monkeypatch):
    bogus = tmp_path / "dispensers.toml"
    bogus.mkdir()
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(bogus))

    with pytest.raises(SystemExit) as exc_info:
        main_mod.load_dispenser_profiles(ConfigModel())
    assert exc_info.value.code == 1


def test_fixture_provides_two_profiles(dispenser_profiles):
    assert dispenser_profiles.profile_for_slot(1).mechanism == "bagged_ice"
    assert dispenser_profiles.profile_for_slot(2).mechanism == "water_fill"


def test_compose_sets_dispensers_env():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    config_count = text.count("ICE_COLDER_CONFIG=/app/data/config.json")
    dispensers_count = text.count("ICE_COLDER_DISPENSERS=/app/data/dispensers.toml")

    assert config_count > 0
    assert dispensers_count == config_count
