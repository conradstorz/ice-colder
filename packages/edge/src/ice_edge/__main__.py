from ice_edge.app.machine import Machine
from ice_edge.hal.command_dispatcher import CommandDispatcher
from ice_common.mqtt_client import MQTTClient
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.notify.notifier import Notifier
from ice_edge.customer_ui.display_controller import DisplayController
from ice_edge.monitoring.inventory_manager import InventoryManager
from ice_edge.ledger.event_recorder import EventRecorder
from ice_edge.faults.availability import Availability
from ice_edge.ledger.session_store import SessionStore
from ice_common.build_info import BUILD_INFO
from ice_common.logging_setup import setup_logging
from ice_edge.notify.mailer import send_email
from ice_edge.reporting import report_scheduler
from ice_edge.app.startup_dispensers import (
    load_dispenser_profiles,
    wire_dispenser_profiles,
)
from ice_edge.app.startup_recovery import reconcile_sales_journal_faults
from ice_common.task_lifecycle import run_until_primary_exits
from ice_common.task_supervisor import supervise

import asyncio
import sys

from loguru import logger

import uvicorn
from ice_edge.access.access import AccessStore
from ice_edge.app.startup_config import (
    apply_env_overrides,
    load_config,
    warn_if_no_buttons,
    warn_if_setup_mode,
)
from ice_edge.uplink.client import HttpUplink
from ice_edge.uplink.identity import MachineIdentity
from ice_edge.uplink.loop import run_uplink
from ice_edge.web.context import holder_display_name
from ice_edge.web.server import app
from ice_edge.web import routes
from ice_edge.web import auth as web_auth


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")
    logger.info(
        f"Build: {BUILD_INFO.commit_short} ({BUILD_INFO.source}, {BUILD_INFO.build_time})"
    )

    live_config = load_config()
    warn_if_no_buttons(live_config)
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
    machine = Machine(config=live_config)
    machine.set_inventory_manager(inventory)
    machine.attach_to_loop(asyncio.get_running_loop())
    routes.set_machine_instance(machine)
    machine.set_session_liveness(access_store.session_is_live)
    routes.set_inventory_manager(inventory)
    logger.info("VMC instance created and attached to event loop")

    # Create health monitor and notifier
    health = HealthMonitor(machine_id=live_config.machine_id)
    notifier = Notifier(config=live_config)
    health.set_alert_callback(notifier.send)
    routes.set_health_monitor(health)
    logger.info("Health monitor and notifier set up and linked")

    availability = Availability()
    machine.set_availability(availability)
    routes.set_availability(availability)
    logger.info("Availability wired to VMC and routes")

    # Create MQTT client and wire it to the VMC
    mqtt = MQTTClient(config=overrides.mqtt, machine_id=live_config.machine_id)

    def _on_mqtt_connection(connected: bool) -> None:
        health.update_mqtt_status(connected)
        machine.on_mqtt_connection(connected)
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
    machine.set_mqtt_client(mqtt)
    machine.set_health_monitor(health)
    logger.info("MQTT client created and linked to VMC and health monitor")

    wire_dispenser_profiles(machine, dispenser_profiles)
    logger.info("Dispenser profiles wired to VMC and routes")

    # Subsystem command channel (system-tests design §2.1): registers its
    # `cmd/+/ack` handler on `mqtt` right away, well before mqtt.run() (in
    # the supervised tasks below) ever connects and subscribes. Handed to
    # the VMC via `Machine.set_command_dispatcher` (added alongside the
    # maintenance-hold wiring; see .superpowers/sdd/task-5-report.md for
    # why this was split from the dispatcher's own construction), and to
    # the routes module via its own setter (Task 13a) -- the Tests level's
    # POST /tests/{subsystem}/{command} (Task 13b) is the first reader on
    # that side.
    dispatcher = CommandDispatcher(mqtt)
    machine.set_command_dispatcher(dispatcher)
    routes.set_command_dispatcher(dispatcher)
    logger.info("Command dispatcher created and registered on cmd/+/ack")

    # Create event recorder and wire to MQTT, VMC, and routes
    recorder = EventRecorder(db_path="data/events.db")
    recorder.register_handlers(mqtt)
    machine.set_event_recorder(recorder)
    routes.set_event_recorder(recorder)
    availability.set_event_recorder(recorder)
    logger.info("Event recorder wired up")
    reconcile_sales_journal_faults(machine, recorder)

    machine.set_session_store(SessionStore())
    logger.info("Session store attached; previous open session checked")

    # Create display controller and wire to MQTT + VMC
    display = DisplayController()
    display.set_mqtt(mqtt, asyncio.get_running_loop())
    machine.set_display_controller(display)
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
    background = [
        supervise("MQTT client", mqtt.run),
        supervise("health monitor", health.run),
        supervise(
            "report scheduler",
            lambda: report_scheduler.run(
                live_config, recorder, send_email, report_scheduler.local_now
            ),
        ),
    ]
    uplink = None
    if live_config.cloud.enabled:
        # Off by default. Uploads run in this supervised task with a 5 s
        # timeout and never block a sale.
        uplink = HttpUplink(live_config.cloud.url, MachineIdentity.load_or_create())
        background.append(
            supervise(
                "uplink",
                lambda: run_uplink(
                    machine, live_config, uplink, holder_name=holder_display_name
                ),
            )
        )
        logger.info(f"Cloud uplink enabled, reporting to {live_config.cloud.url}")
    try:
        await run_until_primary_exits(server.serve(), *background)
    finally:
        if uplink is not None:
            await uplink.aclose()
        await machine.drain_persistence()
        logger.info("Shutdown: drained persistence tasks")
        machine.cancel_pending_tasks()
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
