"""Tests for docker/machine/launcher.py (settings parsing and config seeding)."""

import asyncio
import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

from ice_common.config_model import ConfigModel
from ice_edge.config.config_store import add_product
from ice_edge.vend.dispensers import validate_document
from ice_edge.vend.dispensers_doc import render_example

LAUNCHER = Path(__file__).resolve().parents[2] / "docker" / "machine" / "launcher.py"


@pytest.fixture(scope="module")
def event_loop_policy():
    # tests/firmware/conftest.py installs the selector policy for the whole
    # session on Windows, and that loop cannot spawn subprocesses.
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


spec = importlib.util.spec_from_file_location("machine_launcher", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def _env(tmp_path, **extra):
    env = {"ICE_CLOUD_URL": "http://cloud.test:26124", "ICE_COLDER_DATA": str(tmp_path)}
    env.update(extra)
    return env


def _settings(tmp_path, **extra):
    return launcher.Settings.from_env(_env(tmp_path, **extra), hostname="unit-host")


def test_from_env_requires_cloud_url():
    for env in ({}, {"ICE_CLOUD_URL": ""}):
        with pytest.raises(launcher.LauncherError) as exc:
            launcher.Settings.from_env(env, hostname="unit-host")
        assert "ICE_CLOUD_URL" in str(exc.value)


def test_from_env_defaults():
    s = launcher.Settings.from_env({"ICE_CLOUD_URL": "http://c"}, hostname="unit-host")
    assert s.machine_id == "unit-host"
    assert s.report_interval == 60
    assert s.data_dir == Path("/app/data")


def test_from_env_interval_bounds(tmp_path):
    for bad in ("9", "3601", "abc"):
        with pytest.raises(launcher.LauncherError):
            _settings(tmp_path, ICE_COLDER_REPORT_INTERVAL=bad)
    assert _settings(tmp_path, ICE_COLDER_REPORT_INTERVAL="10").report_interval == 10
    assert (
        _settings(tmp_path, ICE_COLDER_REPORT_INTERVAL="3600").report_interval == 3600
    )


def test_seed_fresh_dir_sells(tmp_path):
    s = _settings(tmp_path, ICE_COLDER_MACHINE_ID="m-1")
    config = launcher.seed(s)
    loaded = ConfigModel.model_validate_json((tmp_path / "config.json").read_text())
    rows = [(p.sku, p.slot, p.kind, p.button) for p in loaded.products]
    assert rows == [
        ("SAMPLE-ICE", 0, "ice", "button_ice"),
        ("SAMPLE-WATER-SM", 1, "water", "button_water_1gal"),
        ("SAMPLE-WATER-LG", 2, "water", "button_water_5gal"),
    ]
    report = validate_document(
        (tmp_path / "dispensers.toml").read_text(), config.products
    )
    assert report.errors == []
    assert loaded.cloud.enabled is True
    assert loaded.cloud.url == "http://cloud.test:26124"
    assert loaded.machine_id == "m-1"
    assert loaded.mqtt.broker_host == "127.0.0.1"


def test_seed_preserves_identity_and_catalog(tmp_path, monkeypatch):
    s = _settings(tmp_path, ICE_COLDER_MACHINE_ID="m-1")
    ident = tmp_path / "cloud_identity.json"
    access = tmp_path / "access.json"
    ident.write_bytes(b"\x00identity\xff")
    access.write_bytes(b"{arbitrary")
    config = launcher.seed(s)
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(tmp_path / "config.json"))
    assert add_product(config, "EXTRA", "Extra", 5.0, slot=3)
    path = tmp_path / "config.json"
    data = json.loads(path.read_text())
    data["machine_id"] = "edited"
    path.write_text(json.dumps(data))
    again = launcher.seed(s)
    assert ident.read_bytes() == b"\x00identity\xff"
    assert access.read_bytes() == b"{arbitrary"
    assert len(again.products) == 4
    assert again.machine_id == "m-1"
    assert ConfigModel.model_validate_json(path.read_text()).machine_id == "m-1"


def test_seed_leaves_existing_dispensers_alone(tmp_path):
    (tmp_path / "dispensers.toml").write_text("# custom\n")
    launcher.seed(_settings(tmp_path))
    assert (tmp_path / "dispensers.toml").read_text() == "# custom\n"


def test_seed_rejects_directory_config(tmp_path):
    (tmp_path / "config.json").mkdir()
    with pytest.raises(launcher.LauncherError):
        launcher.seed(_settings(tmp_path))


def test_seed_rejects_unparsable_config(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    with pytest.raises(launcher.LauncherError):
        launcher.seed(_settings(tmp_path))


def test_child_env(tmp_path):
    s = _settings(tmp_path)
    base = {
        "PATH": "x",
        "MQTT_BROKER_HOST": "h",
        "MQTT_USERNAME": "u",
        "MQTT_PASSWORD": "p",
    }
    env = launcher.child_env(s, base)
    assert env["ICE_COLDER_CONFIG"] == str(tmp_path / "config.json")
    assert env["ICE_COLDER_DISPENSERS"] == str(tmp_path / "dispensers.toml")
    assert env["PATH"] == "x"
    assert not [k for k in env if k.startswith("MQTT_")]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _poll(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def test_wait_for_port_times_out():
    with pytest.raises(launcher.LauncherError):
        await launcher.wait_for_port(
            "127.0.0.1", _free_port(), timeout=0.3, interval=0.05
        )


async def test_wait_for_port_succeeds():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await launcher.wait_for_port("127.0.0.1", port, timeout=2.0, interval=0.05)
    finally:
        server.close()
        await server.wait_closed()


async def test_supervisor_restarts_exiting_child():
    child = launcher.ChildSpec(
        "flaky", (sys.executable, "-c", "import sys; sys.exit(3)")
    )
    sup = launcher.Supervisor([child], env={}, restart_delay=0.05, stop_timeout=5.0)
    task = asyncio.create_task(sup.run())
    assert await _poll(lambda: sup.restarts.get("flaky", 0) >= 2)
    sup.request_stop()
    assert await asyncio.wait_for(task, 10) == 0


async def test_supervisor_fatal_child_returns_1():
    fatal = launcher.ChildSpec(
        "broker", (sys.executable, "-c", "import sys; sys.exit(0)"), fatal=True
    )
    sleeper = launcher.ChildSpec(
        "sleeper", (sys.executable, "-c", "import time; time.sleep(100)")
    )
    sup = launcher.Supervisor(
        [fatal, sleeper], env={}, restart_delay=0.05, stop_timeout=5.0
    )
    assert await asyncio.wait_for(sup.run(), 10) == 1
    assert sup.processes["sleeper"].returncode is not None


async def test_supervisor_start_is_idempotent():
    child = launcher.ChildSpec(
        "sleeper", (sys.executable, "-c", "import time; time.sleep(100)")
    )
    sup = launcher.Supervisor([child], env={}, stop_timeout=5.0)
    await sup.start(child)
    first = sup.processes["sleeper"]
    task = asyncio.create_task(sup.run())
    await asyncio.wait_for(sup.all_started.wait(), 5)
    assert sup.processes["sleeper"] is first
    sup.request_stop()
    assert await asyncio.wait_for(task, 10) == 0


async def test_supervisor_keeps_retrying_failed_spawn(tmp_path):
    ghost = launcher.ChildSpec("ghost", (str(tmp_path / "no-such-binary"),))
    sup = launcher.Supervisor([ghost], env={}, restart_delay=0.05, stop_timeout=5.0)
    task = asyncio.create_task(sup.run())
    # restarts counts spawn attempts, failed ones included
    assert await _poll(lambda: sup.restarts.get("ghost", 0) >= 2)
    assert not task.done()
    sup.request_stop()
    assert await asyncio.wait_for(task, 10) == 0


async def test_wait_for_port_gives_up_when_stopped():
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        launcher.wait_for_port("127.0.0.1", _free_port(), timeout=30, stop=stop), 5
    )


def test_app_children_argv(tmp_path):
    children = launcher.app_children(_settings(tmp_path))
    assert [c.name for c in children] == ["vending", "ice_maker", "mdb", "edge"]
    assert not any(c.fatal for c in children)
    modules = {c.name: c.argv[2] for c in children}
    assert modules == {
        "vending": "ice_sims.vending_machine",
        "ice_maker": "ice_sims.ice_maker",
        "mdb": "ice_sims.mdb_gateway",
        "edge": "ice_edge",
    }
    for c in children:
        assert c.argv[:2] == (sys.executable, "-m")
        if c.name != "edge":
            assert c.argv[3:] == ("--broker", "127.0.0.1")


async def test_amain_missing_url_returns_1(tmp_path, capsys):
    assert await launcher.amain({"ICE_COLDER_DATA": str(tmp_path)}) == 1
    assert "ICE_CLOUD_URL" in capsys.readouterr().out
    assert not (tmp_path / "config.json").exists()


def test_seed_writes_dispensers_atomically(tmp_path):
    launcher.seed(_settings(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "dispensers.toml").read_text(
        encoding="utf-8"
    ) == render_example()


async def test_amain_missing_mosquitto_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "MOSQUITTO", ("ice-no-such-broker-binary",))
    assert await launcher.amain(_env(tmp_path)) == 1
    assert "[launcher] error: cannot start mosquitto:" in capsys.readouterr().out
