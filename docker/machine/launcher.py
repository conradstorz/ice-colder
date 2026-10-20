"""Simulated-machine container launcher.

Spec: docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md
(sections 4.1 environment, 4.2 seed, 4.3 start order and supervision). This module
holds the settings parsing, idempotent config seeding and process supervision.
"""

import asyncio
import os
import signal
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ice_common.config_model import ConfigModel, Product
from ice_edge.config.config_store import save_config
from ice_edge.vend.dispensers_doc import render_example


class LauncherError(Exception):
    """A configuration problem the operator must fix; main() prints it and exits 1."""


@dataclass(frozen=True)
class Settings:
    cloud_url: str
    machine_id: str
    report_interval: int
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> "Settings":
        cloud_url = env.get("ICE_CLOUD_URL", "").strip()
        if not cloud_url:
            raise LauncherError(
                "ICE_CLOUD_URL is required (base URL of the cloud server)"
            )
        machine_id = env.get("ICE_COLDER_MACHINE_ID", "").strip() or hostname
        raw = env.get("ICE_COLDER_REPORT_INTERVAL", "").strip() or "60"
        try:
            interval = int(raw)
        except ValueError:
            raise LauncherError(
                f"ICE_COLDER_REPORT_INTERVAL must be an integer 10..3600, got {raw!r}"
            ) from None
        if not 10 <= interval <= 3600:
            raise LauncherError(
                f"ICE_COLDER_REPORT_INTERVAL must be within 10..3600, got {interval}"
            )
        data_dir = Path(env.get("ICE_COLDER_DATA", "").strip() or "/app/data")
        return cls(cloud_url, machine_id, interval, data_dir)


DEFAULT_PRODUCTS: tuple[dict, ...] = (
    {
        "sku": "SAMPLE-ICE",
        "name": "Sample Ice",
        "slot": 0,
        "kind": "ice",
        "button": "button_ice",
        "price": 3.00,
    },
    {
        "sku": "SAMPLE-WATER-SM",
        "name": "Sample Water Sm",
        "slot": 1,
        "kind": "water",
        "button": "button_water_1gal",
        "price": 1.00,
    },
    {
        "sku": "SAMPLE-WATER-LG",
        "name": "Sample Water Lg",
        "slot": 2,
        "kind": "water",
        "button": "button_water_5gal",
        "price": 2.00,
    },
)


def _load_or_default(config_path: Path) -> ConfigModel:
    if config_path.is_dir():
        raise LauncherError(f"{config_path} is a directory, expected a file")
    if not config_path.exists():
        config = ConfigModel()
        config.physical.products = [Product(**row) for row in DEFAULT_PRODUCTS]
        return config
    try:
        return ConfigModel.model_validate_json(config_path.read_text(encoding="utf-8"))
    except (ValueError, ValidationError, OSError) as exc:
        raise LauncherError(f"cannot parse {config_path}: {exc}") from exc


def seed(settings: Settings) -> ConfigModel:
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    config = _load_or_default(config_path)

    dispensers = data_dir / "dispensers.toml"
    if not dispensers.exists():
        dispensers.write_text(render_example(), encoding="utf-8")

    config.machine_id = settings.machine_id
    config.cloud.enabled = True
    config.cloud.url = settings.cloud_url
    config.cloud.report_interval_seconds = settings.report_interval
    config.mqtt.broker_host = "127.0.0.1"
    config.mqtt.username = None
    config.mqtt.password = None
    save_config(config, config_path)
    return config


def child_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in base.items()
        if k not in ("MQTT_BROKER_HOST", "MQTT_USERNAME", "MQTT_PASSWORD")
    }
    env["ICE_COLDER_CONFIG"] = str(settings.data_dir / "config.json")
    env["ICE_COLDER_DISPENSERS"] = str(settings.data_dir / "dispensers.toml")
    return env


MOSQUITTO_CONF = "/app/machine/mosquitto.conf"
BROKER = ("127.0.0.1", 1883)
RESTART_DELAY = 5.0
STOP_TIMEOUT = 10.0


def log(message: str) -> None:
    print(f"[launcher] {message}", flush=True)


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: tuple[str, ...]
    fatal: bool = False


def app_children(settings: Settings) -> tuple[ChildSpec, ...]:
    def sim(name: str, module: str) -> ChildSpec:
        return ChildSpec(name, (sys.executable, "-m", module, "--broker", BROKER[0]))

    return (
        sim("vending", "ice_sims.vending_machine"),
        sim("ice_maker", "ice_sims.ice_maker"),
        sim("mdb", "ice_sims.mdb_gateway"),
        ChildSpec("edge", (sys.executable, "-m", "ice_edge")),
    )


async def wait_for_port(
    host: str, port: int, *, timeout: float, interval: float = 0.25
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            if loop.time() >= deadline:
                raise LauncherError(
                    f"{host}:{port} did not accept connections within {timeout}s"
                ) from None
            await asyncio.sleep(interval)
        else:
            writer.close()
            return


class Supervisor:
    """Runs and restarts child processes.

    All specs are given at construction. `start(spec)` launches one spec and is
    idempotent per name, so a caller can start a gate-keeping child (mosquitto)
    alone, wait for it, then call `run()`, which first starts every spec not yet
    started and then supervises them all. Public state: `restarts` (restart count
    per name) and `processes` (the current process per name).
    """

    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        env: Mapping[str, str],
        restart_delay: float = RESTART_DELAY,
        stop_timeout: float = STOP_TIMEOUT,
    ) -> None:
        self.specs = tuple(specs)
        self.env = dict(env)
        self.restart_delay = restart_delay
        self.stop_timeout = stop_timeout
        self.restarts: dict[str, int] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()
        self._fatal = asyncio.Event()

    async def _spawn(self, spec: ChildSpec) -> None:
        self.processes[spec.name] = await asyncio.create_subprocess_exec(
            *spec.argv, env=self.env
        )
        log(f"started {spec.name} (pid {self.processes[spec.name].pid})")

    async def start(self, spec: ChildSpec) -> None:
        if spec.name in self._watchers:
            return
        self.restarts.setdefault(spec.name, 0)
        await self._spawn(spec)
        self._watchers[spec.name] = asyncio.create_task(self._watch(spec))

    async def _watch(self, spec: ChildSpec) -> None:
        while not self._stopping.is_set():
            code = await self.processes[spec.name].wait()
            if self._stopping.is_set():
                return
            log(f"{spec.name} exited with code {code}")
            if spec.fatal:
                self._fatal.set()
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), self.restart_delay)
                return
            except asyncio.TimeoutError:
                pass
            await self._spawn(spec)
            self.restarts[spec.name] += 1

    async def run(self) -> int:
        for spec in self.specs:
            await self.start(spec)
        stop_wait = asyncio.create_task(self._stopping.wait())
        fatal_wait = asyncio.create_task(self._fatal.wait())
        try:
            await asyncio.wait(
                {stop_wait, fatal_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_wait.cancel()
            fatal_wait.cancel()
        code = 1 if self._fatal.is_set() and not self._stopping.is_set() else 0
        if code:
            log("fatal child exited; stopping everything")
        await self.stop()
        return code

    def request_stop(self) -> None:
        self._stopping.set()

    async def stop(self) -> None:
        self._stopping.set()
        for watcher in self._watchers.values():
            watcher.cancel()
        live = [p for p in self.processes.values() if p.returncode is None]
        for proc in live:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if live:
            _, pending = await asyncio.wait(
                [asyncio.create_task(p.wait()) for p in live],
                timeout=self.stop_timeout,
            )
            for task in pending:
                task.cancel()
            for proc in live:
                if proc.returncode is None:
                    log(f"killing survivor (pid {proc.pid})")
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
        log("all children stopped")


async def amain(env: Mapping[str, str]) -> int:
    try:
        settings = Settings.from_env(env, hostname=socket.gethostname())
        seed(settings)
        mosquitto = ChildSpec(
            "mosquitto", ("mosquitto", "-c", MOSQUITTO_CONF), fatal=True
        )
        supervisor = Supervisor(
            (mosquitto, *app_children(settings)), env=child_env(settings, env)
        )
        await supervisor.start(mosquitto)
        try:
            await wait_for_port(*BROKER, timeout=10)
        except LauncherError:
            await supervisor.stop()
            raise
    except LauncherError as exc:
        log(f"error: {exc}")
        return 1
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, supervisor.request_stop)
    return await supervisor.run()


def main() -> None:
    raise SystemExit(asyncio.run(amain(os.environ)))


if __name__ == "__main__":
    main()
