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
from services.dispensers import (
    DispenserProfiles,
    Finding,
    ValidationReport,
    dispensers_path,
)
from services.build_info import BUILD_INFO
from services.logging_setup import setup_logging
from services.mailer import send_email
from services import report_scheduler
from services.task_lifecycle import run_until_primary_exits
from services.task_supervisor import supervise

import asyncio
import sys
from datetime import datetime

from loguru import logger

import uvicorn
from config.config_model import ConfigModel
from contracts.vending_machine import FaultCode
from services.access import AccessStore
from services.startup_config import apply_env_overrides, load_config
from web_interface.server import app
from web_interface import routes
from web_interface import auth as web_auth


# Plan 1 (this task) only loads `dispensers.toml` and logs its validation
# report; plan 2 hands this instance to the VMC and routes (CFG-101/CFG-102
# reconciliation, product gating). Kept module-level so plan 2's wiring can
# reach it without a second load.
dispenser_profiles: DispenserProfiles | None = None


def load_dispenser_profiles(config: ConfigModel) -> DispenserProfiles:
    """
    Load `dispensers.toml` (path from `ICE_COLDER_DISPENSERS`, default
    `dispensers.toml`) against `config`'s product catalog and log the
    resulting `ValidationReport`: each finding at `warning` or `error`
    per its own severity, then the report's verdict line at `info`.

    A missing file is a single warning finding (dispensers.py already
    turns it into one) — logged and swallowed, since no product gating
    happens here yet (plan 2). A directory at that path mirrors
    `load_config`'s own directory check: log a clear error and
    `sys.exit(1)` rather than paper over it.

    Extracted from `main()` so it can be exercised in a test without an
    event loop; stores the result on the module-level `dispenser_profiles`
    for plan 2 to pick up.
    """
    global dispenser_profiles
    path = dispensers_path()
    logger.info(f"Loading dispenser profiles from '{path}'")

    profiles = DispenserProfiles(config, path=path)
    try:
        report = profiles.load()
    except IsADirectoryError:
        logger.error(
            f"Dispensers path '{path}' is a directory, not a file. This "
            "typically happens when a Docker bind-mount targets a file "
            "path that doesn't exist yet on the host, so Docker creates a "
            "directory there instead. Remove the directory and fix the "
            "bind-mount/ICE_COLDER_DISPENSERS setting, then retry."
        )
        sys.exit(1)
    except Exception as exc:
        # Anything else out of load() (a pathological TOML file blowing
        # the recursion limit, an unreadable file slipping past
        # DispenserProfiles' own OSError handling, ...) must never crash
        # startup -- a bad dispensers.toml should cost dispenser profiles,
        # never the whole machine.
        logger.error(f"dispensers.toml could not be loaded: {exc}")
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        profiles.report = ValidationReport(
            findings=[
                Finding(
                    slot=None,
                    path="",
                    line=None,
                    severity="error",
                    message=f"dispensers.toml could not be loaded: {first_line}",
                )
            ],
            file_error=True,
        )
        dispenser_profiles = profiles
        return profiles

    for finding in report.findings:
        prefix = "File" if finding.slot is None else f"Slot {finding.slot}"
        path_part = f" › {finding.path}" if finding.path else ""
        line_part = f" (line {finding.line})" if finding.line is not None else ""
        text = f"{prefix}{path_part}{line_part}: {finding.message}"
        if finding.severity == "warning":
            logger.warning(text)
        else:
            logger.error(text)

    logger.info(f"Dispenser profiles: {report.render_text().splitlines()[-1]}")

    dispenser_profiles = profiles
    return profiles


def wire_dispenser_profiles(vmc: VMC, profiles: DispenserProfiles) -> None:
    """Hand the loaded dispenser profiles to the VMC (CFG-101/CFG-102
    reconciliation) and to the routes module (plan 2). Extracted out of
    `main()` so this step can be exercised in a test without an event
    loop -- `main()` itself is an infinite event loop under
    `@logger.catch()`, so it cannot be run partially; this is the same
    two calls `main()` makes, just moved into a function, and changes
    none of `main()`'s own behaviour.
    """
    vmc.set_dispenser_profiles(profiles)
    routes.set_dispenser_profiles(profiles)


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


def _local_now() -> datetime:
    """Clock for the report scheduler: an aware, local-timezone `datetime`.

    A plain wall-clock read -- unlike a test's fake clock, this never
    raises, matching report_scheduler.run's contract (see its docstring).
    """
    return datetime.now().astimezone()


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")
    logger.info(
        f"Build: {BUILD_INFO.commit_short} ({BUILD_INFO.source}, {BUILD_INFO.build_time})"
    )

    live_config = load_config()
    load_dispenser_profiles(live_config)
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
    vmc.set_session_liveness(access_store.session_is_live)
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

    # dispenser_profiles is the module global load_dispenser_profiles set
    # above (~line 454), before overrides/VMC construction -- it is never
    # None by this point.
    assert dispenser_profiles is not None
    wire_dispenser_profiles(vmc, dispenser_profiles)
    logger.info("Dispenser profiles wired to VMC and routes")

    # Subsystem command channel (system-tests design §2.1): registers its
    # `cmd/+/ack` handler on `mqtt` right away, well before mqtt.run() (in
    # the supervised tasks below) ever connects and subscribes. Handed to
    # the VMC via `VMC.set_command_dispatcher` (added alongside the
    # maintenance-hold wiring; see .superpowers/sdd/task-5-report.md for
    # why this was split from the dispatcher's own construction), and to
    # the routes module via its own setter (Task 13a) -- the Tests level's
    # POST /tests/{subsystem}/{command} (Task 13b) is the first reader on
    # that side.
    dispatcher = CommandDispatcher(mqtt)
    vmc.set_command_dispatcher(dispatcher)
    routes.set_command_dispatcher(dispatcher)
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
        await run_until_primary_exits(
            server.serve(),
            supervise("MQTT client", mqtt.run),
            supervise("health monitor", health.run),
            supervise(
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
