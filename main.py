from controller.vmc import VMC
from services.mqtt_client import MQTTClient
from services.health_monitor import HealthMonitor
from services.notifier import Notifier
from services.display_controller import DisplayController
from services.inventory_manager import InventoryManager

import asyncio
import json
import os
import sys

from loguru import logger
from pydantic import ValidationError

import uvicorn
from config.config_model import ConfigModel
from web_interface.server import app
from web_interface import routes

def setup_logging():
    """
    Set up logging configuration for the application.
    """
    # Create the LOGS subdirectory if it doesn't exist
    os.makedirs("LOGS", exist_ok=True)

    # Remove any default logging handlers
    logger.remove()
    # log file with rotation and retention settings
    logger.add(
        "LOGS/vmc.log",
        serialize=False,
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{message};{level} {time:YYYY-MM-DD HH:mm:ss}"
    )
    # Add console logging for INFO and ERROR messages (plain text, with custom format)
    logger.add(
        sys.stdout,
        level="INFO",
        serialize=False,
        format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}"
    )
    # Transaction log — customer interactions only (button, payment, dispense, refund)
    logger.add(
        "LOGS/transactions.log",
        filter=lambda record: record["extra"].get("transaction", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    # Ice maker log — power cycles, ice drops, and out-of-spec behavior
    logger.add(
        "LOGS/ice_maker.log",
        filter=lambda record: record["extra"].get("ice_maker", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    # Vending machine log — button presses, dispense sequences, hardware events
    logger.add(
        "LOGS/vending.log",
        filter=lambda record: record["extra"].get("vending", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )


def _generate_skeleton():
    """Write a skeleton config.json with masked secrets and exit."""
    skeleton = ConfigModel()
    # Serialize with secrets masked so they aren't written in plain text
    json_text = skeleton.model_dump_json(indent=4)
    with open("config.json", "w", encoding="utf-8") as fw:
        fw.write(json_text)
    logger.info("Created skeleton 'config.json' with default values")
    print("Created skeleton config.json — please edit and rerun.")
    sys.exit(0)


def load_config() -> ConfigModel:
    """
    Load configuration from config.json.

    Pydantic fills in defaults for any missing fields — no manual merge needed.
    The user's file is never overwritten.
    """
    logger.info("Loading configuration from 'config.json'")

    if not os.path.exists("config.json"):
        logger.warning("'config.json' not found, creating skeleton")
        _generate_skeleton()

    try:
        with open("config.json", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.exception(f"Error reading 'config.json': {e}")
        sys.exit(1)

    try:
        config_model = ConfigModel.model_validate(raw)
        logger.info(f"Configuration loaded successfully: version={config_model.version}")
    except ValidationError as ve:
        logger.error("Configuration validation failed:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get("loc", []))  # noqa: E741
            logger.error(f"  {loc}: {err.get('msg', '')}")
        sys.exit(1)

    return config_model


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")

    live_config = load_config()
    logger.debug(f"Configuration model: {live_config}")
    logger.info(f"Loaded configuration with version: {getattr(live_config, 'version', 'N/A')}")

    # Wire up configuration, inventory, and VMC for the web routes
    routes.set_config_object(live_config)
    inventory = InventoryManager(live_config.products)
    vmc = VMC(config=live_config)
    vmc.set_inventory_manager(inventory)
    vmc.attach_to_loop(asyncio.get_running_loop())
    routes.set_vmc_instance(vmc)
    logger.info(f"VMC instance created and attached to event loop")

    # Create health monitor and notifier
    health = HealthMonitor()
    notifier = Notifier(config=live_config)
    health.set_alert_callback(notifier.send)
    routes.set_health_monitor(health)
    logger.info(f"Health monitor and notifier set up and linked")

    # Create MQTT client and wire it to the VMC
    mqtt = MQTTClient(config=live_config.mqtt, machine_id=live_config.machine_id)
    mqtt.set_connection_callback(health.update_mqtt_status)
    vmc.set_mqtt_client(mqtt)
    vmc.set_health_monitor(health)
    logger.info(f"MQTT client created and linked to VMC and health monitor")

    # Create display controller and wire to MQTT + VMC
    display = DisplayController()
    display.set_mqtt(mqtt, asyncio.get_running_loop())
    vmc.set_display_controller(display)
    logger.info(f"Display controller created and linked to MQTT client and VMC")

    logger.info(f"MQTT client configured for broker {live_config.mqtt.broker_host}:{live_config.mqtt.broker_port}")

    # Start uvicorn as an asyncio task (non-blocking)
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=26123, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    logger.info("Starting web interface on http://0.0.0.0:26123")

    # Run the web server, MQTT client, and health monitor concurrently
    logger.info(f"Entering main event loop with web server, MQTT client, and health monitor")
    try:
        await asyncio.gather(server.serve(), mqtt.run(), health.run())
    finally:
        vmc.cancel_pending_tasks()
        logger.info("Shutdown: cancelled pending VMC tasks")


if __name__ == "__main__":
    # Windows requires SelectorEventLoop for aiomqtt (paho-mqtt socket callbacks)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logger.info(f"Starting main application")
    asyncio.run(main())
    logger.info(f"Main application has exited")
    