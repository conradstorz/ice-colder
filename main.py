from controller.vmc import VMC
from services.mqtt_client import MQTTClient

import asyncio
import os
import sys
from loguru import logger
from config.config_model import ConfigModel
import json
from pydantic import ValidationError
import shutil
import time

import uvicorn
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


def _deep_merge(default: dict, source: dict) -> dict:
    """
    Recursively merge source dict on top of default dict.
    """
    merged = {}
    # Merge defaults and source
    for key, val in default.items():
        if key in source:
            if isinstance(val, dict) and isinstance(source[key], dict):
                merged[key] = _deep_merge(val, source[key])
            else:
                merged[key] = source[key]
        else:
            merged[key] = val
    # Include any extra keys from source
    for key, val in source.items():
        if key not in merged:
            merged[key] = val
    return merged


def _defaults_applied(orig: dict, merged: dict) -> bool:
    """
    Detect if merged contains keys not in orig (i.e., defaults applied).
    """
    for key in merged:
        if key not in orig:
            return True
        if isinstance(merged[key], dict) and isinstance(orig.get(key), dict):
            if _defaults_applied(orig[key], merged[key]):
                return True
    return False

def load_config() -> ConfigModel:
    """
    Load the configuration from a JSON file, applying defaults.
    """
    logger.info("Loading configuration from 'config.json'")
    # load configuration
    logger.debug("Checking for 'config.json' in current directory")
    # Ensure config.json exists; if not, generate a skeleton for user
    def _json_encoder(o):
        from pydantic import SecretStr
        if isinstance(o, SecretStr):
            # expose the actual secret (or o.get_secret_value())—
            # or return "********" if you want to keep it masked
            return o.get_secret_value()
        # for any other unknown types, let it error
        raise TypeError(f"Type {o.__class__.__name__} not serializable")
    if not os.path.exists("config.json"):
        logger.warning("'config.json' not found, creating skeleton with default values")
        skeleton_dict = ConfigModel.model_construct().model_dump()
        json_text = json.dumps(skeleton_dict, default=_json_encoder, indent=4)
        with open("config.json", "w", encoding="utf-8") as fw:
            fw.write(json_text)        
        logger.info("Created skeleton 'config.json' with default values")
        logger.info("Please edit 'config.json' with your configuration settings")
        print("Created skeleton config.json—please edit and rerun.")
        sys.exit(0)
    
    # Load user config
    try:
        logger.debug("Reading raw JSON from 'config.json'")
        orig_data = json.load(open("config.json", encoding="utf-8"))
    except Exception as e:
        logger.exception(f"Error reading 'config.json': {e}")
        sys.exit(1)

    # Build a default config dict from Pydantic model_construct
    default_dict = ConfigModel.model_construct().model_dump()
    logger.debug("Constructed default configuration from Pydantic model")
    logger.debug(f"Default configuration: {default_dict}")
    merged_data = _deep_merge(default_dict, orig_data)
    logger.debug("Merged user configuration with defaults")
    logger.debug(f"Merged configuration: {merged_data}")
    # Ensure merged_data is a valid JSON object
    if not isinstance(merged_data, dict):
        logger.error("Merged configuration is not a valid JSON object")
        sys.exit(1)
    logger.debug("Merged configuration is valid.")

    # Backup and write defaults if any missing keys were added
    if _defaults_applied(orig_data, merged_data):
        logger.info("Default values applied to configuration, backing up original")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = f"config.json.bak_{timestamp}"
        shutil.copy("config.json", backup_path)
        logger.info(f"Backed up original config to {backup_path}")

        json_text = json.dumps(merged_data, default=_json_encoder, indent=4)
        with open("config.json", "w", encoding="utf-8") as fw:
            fw.write(json_text)
            logger.debug("Wrote merged configuration to 'config.json'") 

    # Validate merged config
    try:
        logger.debug("Validating merged configuration via Pydantic model")
        config_model = ConfigModel.model_validate(merged_data)
        version = getattr(config_model, "version", None)
        logger.info(
            "Configuration loaded successfully",
            f": version={version}" if version else ""
        )
    except ValidationError as ve:
        logger.error("Configuration validation failed with the following errors:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get('loc', []))  # noqa: E741
            msg = err.get('msg', '')
            logger.error(f"  • {loc}: {msg}")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error validating configuration")
        sys.exit(1)
    return config_model


@logger.catch()
async def main():
    setup_logging()
    logger.info("Starting Vending Machine Controller")

    live_config = load_config()
    logger.debug(f"Configuration model: {live_config}")

    # Wire up configuration and VMC for the web routes
    routes.set_config_object(live_config)
    vmc = VMC(config=live_config)
    vmc.attach_to_loop(asyncio.get_running_loop())
    routes.set_vmc_instance(vmc)

    # Create MQTT client and wire it to the VMC
    mqtt = MQTTClient(config=live_config.mqtt, machine_id=live_config.machine_id)
    vmc.set_mqtt_client(mqtt)
    logger.info(f"MQTT client configured for broker {live_config.mqtt.broker_host}:{live_config.mqtt.broker_port}")

    # Start uvicorn as an asyncio task (non-blocking)
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    logger.info("Starting web interface on http://0.0.0.0:8000")

    # Run the web server and MQTT client concurrently
    await asyncio.gather(server.serve(), mqtt.run())


if __name__ == "__main__":
    asyncio.run(main())
