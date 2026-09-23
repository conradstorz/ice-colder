from controller.vmc import VMC
from services.mqtt_client import MQTTClient
from services.health_monitor import HealthMonitor
from services.notifier import Notifier
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from services.event_recorder import EventRecorder
from services.availability import Availability
from services.session_store import SessionStore
from services.config_store import save_config
from services.build_info import BUILD_INFO
from services.paths import LOG_DIR, LOG_FILE

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pydantic import SecretStr, ValidationError

import uvicorn
from config.config_model import ConfigModel, MQTTConfig
from services.auth_policy import generate_admin_password, is_loopback, password_problem
from web_interface.server import app
from web_interface import routes


def setup_logging():
    """
    Set up logging configuration for the application.
    """
    # Create the LOGS subdirectory if it doesn't exist
    os.makedirs(LOG_DIR, exist_ok=True)

    # Remove any default logging handlers
    logger.remove()
    # log file with rotation and retention settings
    logger.add(
        str(LOG_FILE),
        serialize=False,
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{message};{level} {time:YYYY-MM-DD HH:mm:ss}",
    )
    # Console: one line per record, level before the message so greps attribute it correctly
    logger.add(
        sys.stdout,
        level="INFO",
        serialize=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    )
    # Transaction log — customer interactions only (button, payment, dispense, refund)
    logger.add(
        str(LOG_DIR / "transactions.log"),
        filter=lambda record: record["extra"].get("transaction", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    # Ice maker log — power cycles, ice drops, and out-of-spec behavior
    logger.add(
        str(LOG_DIR / "ice_maker.log"),
        filter=lambda record: record["extra"].get("ice_maker", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    # Vending machine log — button presses, dispense sequences, hardware events
    logger.add(
        str(LOG_DIR / "vending.log"),
        filter=lambda record: record["extra"].get("vending", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )


def _config_path() -> str:
    """Resolve the active config path from ``ICE_COLDER_CONFIG`` (read at call
    time so tests can monkeypatch env and cwd independently), defaulting to
    ``config.json`` in the current working directory — unchanged behavior for
    local runs and tests.
    """
    return os.environ.get("ICE_COLDER_CONFIG", "config.json")


def _create_default_config(path: str) -> ConfigModel:
    """First run: blank defaults plus a random admin password, persisted, then continue."""
    defaults = ConfigModel()
    password = generate_admin_password()
    defaults.web.admin_password = SecretStr(password)
    save_config(defaults, Path(path))
    logger.info(f"First run: created '{path}' with blank defaults")
    logger.warning(
        f"First run: dashboard login is {defaults.web.admin_username} / {password} "
        "— change it in config.json"
    )
    return defaults


def load_config() -> ConfigModel:
    """
    Load configuration from the path named by ``ICE_COLDER_CONFIG`` (default
    ``config.json``).

    Pydantic fills in defaults for any missing fields — no manual merge needed.
    The user's file is never overwritten.
    """
    path = _config_path()
    logger.info(f"Loading configuration from '{path}'")

    if os.path.isdir(path):
        logger.error(
            f"Config path '{path}' is a directory, not a file. This typically "
            "happens when a Docker bind-mount targets a file path that doesn't "
            "exist yet on the host, so Docker creates a directory there instead. "
            "Remove the directory and fix the bind-mount/ICE_COLDER_CONFIG "
            "setting, then retry."
        )
        sys.exit(1)

    if not os.path.exists(path):
        logger.warning(f"'{path}' not found — first run: creating defaults")
        return _create_default_config(path)

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.exception(f"Error reading '{path}': {e}")
        sys.exit(1)

    try:
        config_model = ConfigModel.model_validate(raw)
        logger.info(
            f"Configuration loaded successfully: version={config_model.version}"
        )
    except ValidationError as ve:
        logger.error("Configuration validation failed:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get("loc", []))  # noqa: E741
            logger.error(f"  {loc}: {err.get('msg', '')}")
        sys.exit(1)

    return config_model


@dataclass
class EnvOverrides:
    """Env-derived values that must not be written back to config.json.

    ``mqtt`` is a copy of ``config.mqtt`` with env values layered on top;
    ``trusted_proxies`` is the env list if set, else the config's own.
    """

    mqtt: MQTTConfig
    trusted_proxies: list[str]


def apply_env_overrides(config: ConfigModel) -> EnvOverrides:
    """Docker-friendly overrides: broker host/credentials and trusted proxies.

    Returns an ``EnvOverrides`` built from a copy of ``config.mqtt`` — the
    live ``config`` is never mutated, so a later ``save_config(config)`` (the
    inventory routes do this) can never persist an env-only secret like
    ``MQTT_PASSWORD`` into config.json or its ``.bak``.

    Read at call time so tests can monkeypatch the environment.
    """
    mqtt = config.mqtt.model_copy(deep=True)

    host = os.environ.get("MQTT_BROKER_HOST")
    if host:
        mqtt.broker_host = host
        logger.info(f"MQTT broker host overridden by env: {host}")
    username = os.environ.get("MQTT_USERNAME")
    if username:
        mqtt.username = username
        logger.info(f"MQTT username overridden by env: {username}")
    password = os.environ.get("MQTT_PASSWORD")
    if password:
        mqtt.password = SecretStr(password)

    proxies_env = os.environ.get("ICE_COLDER_TRUSTED_PROXIES")
    if proxies_env:
        trusted_proxies = [p.strip() for p in proxies_env.split(",") if p.strip()]
        logger.info(f"Trusted proxies overridden by env: {trusted_proxies}")
    else:
        trusted_proxies = list(config.web.trusted_proxies)

    return EnvOverrides(mqtt=mqtt, trusted_proxies=trusted_proxies)


def enforce_password_policy(web) -> None:
    """Refuse to serve a weak admin password on a non-loopback interface.

    ICE_COLDER_ALLOW_WEAK_PASSWORD=1 downgrades the refusal to a warning; it is
    for a local shell or an uncommitted compose override, never the committed
    stack.
    """
    problem = password_problem(web.admin_password.get_secret_value())
    if problem is None:
        return
    if is_loopback(web.host):
        logger.warning(f"Dashboard on loopback with a weak password ({problem})")
        return
    if os.environ.get("ICE_COLDER_ALLOW_WEAK_PASSWORD") == "1":
        logger.warning(
            f"ICE_COLDER_ALLOW_WEAK_PASSWORD=1: serving on {web.host} although {problem}"
        )
        return
    logger.error(
        f"Refusing to serve the dashboard on {web.host}: {problem}. "
        "Set web.admin_password in config.json to at least 12 characters, "
        "or bind web.host to 127.0.0.1, or set ICE_COLDER_ALLOW_WEAK_PASSWORD=1 "
        "for a private test host."
    )
    sys.exit(1)


_SUPERVISE_RESTART_DELAY = 5.0


async def _run_until_server_exits(server_coro, *supervised):
    """Run ``server_coro`` (uvicorn's ``server.serve()``) alongside long-running
    ``supervised`` background coroutines (the MQTT client / health monitor
    supervisors). Returns (or raises) as soon as ``server_coro`` completes,
    cancelling the still-running supervised tasks first.

    Without this, ``asyncio.gather`` over the server plus supervisors that loop
    forever never returns when uvicorn exits (SIGTERM/SIGINT, or a startup
    failure) — ``main()`` never reaches its ``finally`` block and the process
    never exits, so Docker's ``restart: unless-stopped`` never gets a chance to
    restart it.
    """
    server_task = asyncio.ensure_future(server_coro)
    supervised_tasks = [asyncio.ensure_future(c) for c in supervised]
    try:
        return await server_task
    finally:
        for task in supervised_tasks:
            task.cancel()
        for task in supervised_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _supervise(name: str, coro_factory):
    """Keep a long-running component alive: log a crash and restart it after 5s.

    Prevents one component's unhandled exception from unwinding asyncio.gather
    and taking down the whole VMC process.
    """
    while True:
        try:
            await coro_factory()
            logger.warning(f"{name} exited unexpectedly; restarting in 5s")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{name} crashed; restarting in 5s")
        await asyncio.sleep(_SUPERVISE_RESTART_DELAY)


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")
    logger.info(
        f"Build: {BUILD_INFO.commit_short} ({BUILD_INFO.source}, {BUILD_INFO.build_time})"
    )

    live_config = load_config()
    overrides = apply_env_overrides(live_config)
    logger.debug(f"Configuration model: {live_config}")
    logger.info(
        f"Loaded configuration with version: {getattr(live_config, 'version', 'N/A')}"
    )

    # Wire up configuration, inventory, and VMC for the web routes
    routes.set_config_object(live_config)
    # set_config_object above seeds the limiter from live_config.web.trusted_proxies
    # (empty unless the operator set it in config.json) — apply the env override
    # after, so ICE_COLDER_TRUSTED_PROXIES takes effect without ever touching
    # live_config itself.
    routes.login_limiter.set_trusted_proxies(overrides.trusted_proxies)
    inventory = InventoryManager(live_config.products)
    vmc = VMC(config=live_config)
    vmc.set_inventory_manager(inventory)
    vmc.attach_to_loop(asyncio.get_running_loop())
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inventory)
    logger.info("VMC instance created and attached to event loop")

    # Create health monitor and notifier
    health = HealthMonitor(machine_id=live_config.machine_id)
    notifier = Notifier(config=live_config)
    health.set_alert_callback(notifier.send)
    routes.set_health_monitor(health)
    logger.info("Health monitor and notifier set up and linked")

    availability = Availability(live_config.products)
    vmc.set_availability(availability)
    routes.set_availability(availability)
    logger.info("Availability wired to VMC and routes")

    # Create MQTT client and wire it to the VMC
    mqtt = MQTTClient(config=overrides.mqtt, machine_id=live_config.machine_id)

    def _on_mqtt_connection(connected: bool) -> None:
        health.update_mqtt_status(connected)
        vmc.on_mqtt_connection(connected)

    mqtt.set_connection_callback(_on_mqtt_connection)
    vmc.set_mqtt_client(mqtt)
    vmc.set_health_monitor(health)
    logger.info("MQTT client created and linked to VMC and health monitor")

    # Create event recorder and wire to MQTT, VMC, and routes
    recorder = EventRecorder(db_path="data/events.db")
    recorder.register_handlers(mqtt)
    vmc.set_event_recorder(recorder)
    routes.set_event_recorder(recorder)
    availability.set_event_recorder(recorder)
    logger.info("Event recorder wired up")

    vmc.set_session_store(SessionStore())
    logger.info("Session store attached; previous open session checked")

    # Create display controller and wire to MQTT + VMC
    display = DisplayController()
    display.set_mqtt(mqtt, asyncio.get_running_loop())
    vmc.set_display_controller(display)
    logger.info("Display controller created and linked to MQTT client and VMC")

    logger.info(
        f"MQTT client configured for broker {overrides.mqtt.broker_host}:{overrides.mqtt.broker_port}"
    )

    # Start uvicorn as an asyncio task (non-blocking)
    web_cfg = live_config.web
    enforce_password_policy(web_cfg)
    uvicorn_config = uvicorn.Config(
        app, host=web_cfg.host, port=web_cfg.port, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)
    logger.info(f"Starting web interface on http://{web_cfg.host}:{web_cfg.port}")

    # Run the web server, MQTT client, and health monitor concurrently
    logger.info(
        "Entering main event loop with web server, MQTT client, and health monitor"
    )
    try:
        await _run_until_server_exits(
            server.serve(),
            _supervise("MQTT client", mqtt.run),
            _supervise("health monitor", health.run),
        )
    finally:
        await vmc.drain_persistence()
        logger.info("Shutdown: drained persistence tasks")
        vmc.cancel_pending_tasks()
        logger.info("Shutdown: cancelled pending VMC tasks")
        recorder.flush()
        logger.info("Shutdown: flushed event recorder")


if __name__ == "__main__":
    # Windows requires SelectorEventLoop for aiomqtt (paho-mqtt socket callbacks)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logger.info("Starting main application")
    asyncio.run(main())
    logger.info("Main application has exited")
