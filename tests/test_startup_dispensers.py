# tests/test_startup_dispensers.py
"""Tests for `services/startup_dispensers.py` -- startup wiring for
`dispensers.toml`. `load_dispenser_profiles(config)` loads and logs a
`DispenserProfiles` report without an event loop; a directory at the
dispensers path mirrors `load_config`'s own directory-exit behaviour.
`wire_dispenser_profiles(vmc, profiles)` hands the loaded profiles to both
the VMC and the routes module.
"""

import ast

import pytest
import yaml

import services.startup_dispensers as startup_dispensers
from ice_common.config_model import ConfigModel
from ice_edge.vend.dispensers import DispenserProfiles
from tests.paths import REPO_ROOT

COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


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


def test_wire_dispenser_profiles_reaches_machine_gate_and_routes():
    """One load reaches both consumers with the SAME object, through a real
    `Machine` (nothing on the Machine side is mocked): the public
    `machine.gate.profiles` read and the routes-side `context` handle."""
    from controller.machine import Machine
    from ice_edge.web import context, routes

    cfg = ConfigModel()
    machine = Machine(config=cfg)
    sentinel = DispenserProfiles(cfg)
    assert machine.gate.profiles is None

    startup_dispensers.wire_dispenser_profiles(machine, sentinel)

    assert machine.gate.profiles is sentinel
    assert context.dispenser_profiles is sentinel
    assert routes.context.dispenser_profiles is sentinel


def test_main_calls_wire_dispenser_profiles_after_set_health_monitor():
    """Structural guard (`main()` is an infinite event loop and cannot be run
    partially): `main()` must pass the very name it assigned from
    `load_dispenser_profiles(...)` to `wire_dispenser_profiles(machine, ...)`,
    and that call must come after `machine.set_health_monitor(...)`."""
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    main_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
    )

    def callee(call):
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)

    loaded_names = [
        t.id
        for n in ast.walk(main_fn)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and callee(n.value) == "load_dispenser_profiles"
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    assert len(loaded_names) == 1, "main() must assign load_dispenser_profiles(...)"

    calls = [n for n in ast.walk(main_fn) if isinstance(n, ast.Call)]
    wires = [c for c in calls if callee(c) == "wire_dispenser_profiles"]
    assert len(wires) == 1, "main() must call wire_dispenser_profiles exactly once"
    wire = wires[0]
    assert [getattr(a, "id", None) for a in wire.args] == ["machine", loaded_names[0]]

    health_lines = [c.lineno for c in calls if callee(c) == "set_health_monitor"]
    assert health_lines, "main() no longer calls set_health_monitor"
    assert wire.lineno > max(health_lines)


def test_compose_sets_dispensers_env():
    """Only the `vmc` service wires ICE_COLDER_DISPENSERS -- the three
    simulators never read dispensers.toml (the VMC sends the whole slot
    profile in the dispense command, plan 2), so their copy of the env var
    was vestigial. Assert it is set in the vmc service and absent from all
    simulator services."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    # Helper to check if an environment (list or dict) contains the dispensers var
    def has_dispensers_var(env):
        """Check if environment contains ICE_COLDER_DISPENSERS variable."""
        if env is None:
            return False
        if isinstance(env, list):
            return any(
                "ICE_COLDER_DISPENSERS=/app/data/dispensers.toml" in item
                for item in env
            )
        elif isinstance(env, dict):
            return env.get("ICE_COLDER_DISPENSERS") == "/app/data/dispensers.toml"
        return False

    # Assert vmc service has it
    vmc_env = data["services"]["vmc"].get("environment")
    assert has_dispensers_var(vmc_env), (
        "vmc service must have ICE_COLDER_DISPENSERS set"
    )

    # Assert simulators don't have it
    simulator_services = ["sim-ice-maker", "sim-mdb", "sim-vending"]
    for sim_name in simulator_services:
        sim_env = data["services"][sim_name].get("environment")
        assert not has_dispensers_var(sim_env), (
            f"{sim_name} must not have ICE_COLDER_DISPENSERS set"
        )
