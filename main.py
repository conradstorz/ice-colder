from controller.vmc import VMC
from services.command_dispatcher import CommandDispatcher
from services.mqtt_client import MQTTClient
from services.health_monitor import HealthMonitor
from services.notifier import Notifier
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager
from services.event_recorder import EventRecorder
from services.availability import Availability
from services.session_store import SessionStore
from services.build_info import BUILD_INFO
from services.logging_setup import setup_logging
from services.mailer import send_email
from services import report_scheduler
from services.startup_dispensers import load_dispenser_profiles, wire_dispenser_profiles
from services.startup_recovery import reconcile_sales_journal_faults
from services.task_lifecycle import run_until_primary_exits
from services.task_supervisor import supervise

import asyncio
import sys

from loguru import logger

import uvicorn
from services.access import AccessStore
from services.startup_config import (
    apply_env_overrides,
    load_config,
    warn_if_setup_mode,
)
from web_interface.server import app
from web_interface import routes
from web_interface import auth as web_auth


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")
    logger.info(
        f"Build: {BUILD_INFO.commit_short} ({BUILD_INFO.source}, {BUILD_INFO.build_time})"
    )

    live_config = load_config()
    dispenser_profiles = load_dispenser_profiles(live_config)
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
                    live_config, recorder, send_email, report_scheduler.local_now
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
