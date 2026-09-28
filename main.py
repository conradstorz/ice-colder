from controller.vmc import VMC
from services.command_dispatcher import CommandDispatcher
from services.mqtt_client import MQTTClient
from services.health_monitor import HealthMonitor
from services.notifier import Notifier
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from services.event_recorder import EventRecorder
from services import event_recorder as event_recorder_module
from services.availability import Availability
from services.session_store import SessionStore
from services.config_store import save_config
from services.build_info import BUILD_INFO
from services.paths import LOG_DIR, LOG_FILE
from services.mailer import send_email
from services import report_scheduler

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import SecretStr, ValidationError

import uvicorn
from config.config_model import ConfigModel, MQTTConfig
from contracts.vending_machine import FaultCode
from services.access import AccessStore
from web_interface.server import app
from web_interface import routes
from web_interface import auth as web_auth


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
    """First run: blank defaults, persisted, then continue.

    No credential is generated here — authentication lives entirely in
    ``data/access.json`` (services/access.py), created separately and
    walked through the setup wizard at /setup.
    """
    defaults = ConfigModel()
    save_config(defaults, Path(path))
    logger.info(f"First run: created '{path}' with blank defaults")
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


def warn_if_setup_mode(store: AccessStore) -> None:
    """Log the dashboard's access-store health at startup. Never exits: a
    dashboard-only problem must never stop the machine selling.

    - Corrupt ``data/access.json``: logged at error level. The dashboard
      serves an error page on every route, but the VMC and MQTT client are
      unaffected and keep running.
    - No owner yet: logged at warning level — the dashboard is in setup mode
      until someone completes the wizard at /setup on the machine.
    - An owner already exists: silent.
    """
    if store.corrupt:
        logger.error(
            f"Access store at {store.path} is corrupt: the dashboard will "
            "serve an error page on every route. The VMC and MQTT client "
            "are unaffected and keep running."
        )
        return
    if store.setup_mode:
        logger.warning(
            "Dashboard is in setup mode: no owner exists yet. Visit /setup "
            "at the machine to create one."
        )


def reconcile_sales_journal_faults(vmc: VMC, recorder: EventRecorder) -> None:
    """At startup: replay any journalled sales and reconcile DATA-101/DATA-102.

    Never exits and never raises out to the caller — a reports/history
    problem must never stop the VMC or MQTT client (program goal 9), so
    every step here is best-effort and any unexpected failure is logged and
    swallowed rather than propagated.

    - ``DATA-102`` is raised once whenever the recorder reports it quarantined
      a corrupt database on this boot (``recorder.db_was_corrupt``, set by
      ``EventRecorder.__init__``, never by this function).
    - ``DATA-101`` is reconciled from the *journal's own state after replay*,
      never from ``replay_sales_journal()``'s return value: that integer is
      only the count of rows this call actually inserted, and it is ``0``
      both when there was nothing to do and when every row was a duplicate
      or a reject — see its docstring. The unambiguous signal is whether
      ``JOURNAL_PATH`` is now absent or empty (drained: clear the fault) or
      still has content (stuck: raise/keep the fault).
    - ``replay_sales_journal()`` itself can raise (e.g. it commits rows to
      ``sales`` but its final ``os.replace`` rewriting the journal cannot
      complete). That exception is caught here, separately from the outer
      swallow-everything handler, specifically so the journal-state check
      below still runs afterward — otherwise the outer handler would log
      and swallow it before ``DATA-101`` is ever reconciled, leaving the
      operator with no alert even though the journal is still non-empty
      (replayed rows included) and the next boot would have to rediscover
      the same problem from scratch.
    """
    try:
        if recorder.db_was_corrupt:
            detail = (
                recorder.corrupt_backup_path
                or "corrupt event database quarantined at startup"
            )
            vmc.raise_data_fault(FaultCode.DATA_102, outcome=detail)
            logger.error(f"Event database was reset after corruption: {detail}")

        try:
            inserted = recorder.replay_sales_journal()
            if inserted:
                logger.info(f"Sales journal replay: inserted {inserted} row(s)")
        except Exception:
            logger.exception(
                "replay_sales_journal raised (e.g. the journal rewrite could "
                "not complete); falling through to check the journal's "
                "current state so DATA-101 is still raised/retained rather "
                "than silently dropped"
            )

        journal_path = event_recorder_module.JOURNAL_PATH
        drained = (
            not journal_path.exists()
            or not journal_path.read_text(encoding="utf-8").strip()
        )
        if drained:
            vmc.clear_fault(FaultCode.DATA_101.value)
        else:
            vmc.raise_data_fault(
                FaultCode.DATA_101,
                outcome="sales journal not fully drained after replay",
            )
            logger.error(
                "Sales journal still has unresolved rows after replay; "
                "DATA-101 remains set"
            )
    except Exception:
        logger.exception(
            "reconcile_sales_journal_faults failed; continuing startup "
            "regardless (a reports/history problem must never stop the "
            "VMC or MQTT client)"
        )


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


def _local_now() -> datetime:
    """Clock for the report scheduler: an aware, local-timezone `datetime`.

    A plain wall-clock read -- unlike a test's fake clock, this never
    raises, matching report_scheduler.run's contract (see its docstring).
    """
    return datetime.now().astimezone()


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
    access_store = AccessStore()
    routes.set_access_store(access_store)
    # trusted_proxies is env-overridable (ICE_COLDER_TRUSTED_PROXIES) — apply
    # the resolved list to the back-off's client-IP keying after the access
    # store is wired, so it takes effect without ever touching live_config.
    web_auth.backoff.set_trusted_proxies(overrides.trusted_proxies)
    warn_if_setup_mode(access_store)
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

    availability = Availability()
    vmc.set_availability(availability)
    routes.set_availability(availability)
    logger.info("Availability wired to VMC and routes")

    # Create MQTT client and wire it to the VMC
    mqtt = MQTTClient(config=overrides.mqtt, machine_id=live_config.machine_id)

    def _on_mqtt_connection(connected: bool) -> None:
        health.update_mqtt_status(connected)
        vmc.on_mqtt_connection(connected)
        if connected:
            # Anything published while disconnected (most notably the setup
            # code, if this is a fresh boot: ensure_setup_mode() below runs
            # before this callback ever fires) was silently dropped by
            # MQTTClient.publish()'s own "not connected" guard — republish
            # once connected, and again on every reconnect, so the display
            # is never left without whatever it should currently show
            # (Copilot review, main.py:349).
            display.republish()

    mqtt.set_connection_callback(_on_mqtt_connection)
    vmc.set_mqtt_client(mqtt)
    vmc.set_health_monitor(health)
    logger.info("MQTT client created and linked to VMC and health monitor")

    # Subsystem command channel (system-tests design §2.1): registers its
    # `cmd/+/ack` handler on `mqtt` right away, well before mqtt.run() (in
    # the supervised tasks below) ever connects and subscribes. Task 5's
    # scope is limited to main.py, contracts/common.py (already landed) and
    # services/command_dispatcher.py — handing this instance to the VMC or
    # to the routes needs a new setter on each (e.g. `VMC.
    # set_command_dispatcher` / `web_interface.context.
    # set_command_dispatcher`), and neither exists yet. Adding one would
    # mean editing controller/vmc.py or a web_interface/routes module,
    # both out of this task's file scope, so that hookup is left to the
    # task that adds the maintenance-hold/Tests-level wiring — see
    # .superpowers/sdd/task-5-report.md.
    CommandDispatcher(mqtt)
    logger.info("Command dispatcher created and registered on cmd/+/ack")

    # Create event recorder and wire to MQTT, VMC, and routes
    recorder = EventRecorder(db_path="data/events.db")
    recorder.register_handlers(mqtt)
    vmc.set_event_recorder(recorder)
    routes.set_event_recorder(recorder)
    availability.set_event_recorder(recorder)
    logger.info("Event recorder wired up")
    reconcile_sales_journal_faults(vmc, recorder)

    vmc.set_session_store(SessionStore())
    logger.info("Session store attached; previous open session checked")

    # Create display controller and wire to MQTT + VMC
    display = DisplayController()
    display.set_mqtt(mqtt, asyncio.get_running_loop())
    vmc.set_display_controller(display)
    logger.info("Display controller created and linked to MQTT client and VMC")

    # Only now can setup mode reach the customer display — ensure_setup_mode()
    # publishes the setup code there when no owner exists yet, so it must run
    # after set_display_controller, not before.
    routes.set_display_controller(display)
    routes.ensure_setup_mode()

    logger.info(
        f"MQTT client configured for broker {overrides.mqtt.broker_host}:{overrides.mqtt.broker_port}"
    )

    # Start uvicorn as an asyncio task (non-blocking)
    web_cfg = live_config.web
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
            _supervise(
                "report scheduler",
                lambda: report_scheduler.run(
                    live_config, recorder, send_email, _local_now
                ),
            ),
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
