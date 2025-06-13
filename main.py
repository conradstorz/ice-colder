import os
import sys
from loguru import logger
from config.config_model import ConfigModel
# from hardware.tkinter_ui import VendingMachineUI  # removed in favor of local webserver dashboard
import json  # For loading configuration
from pydantic import ValidationError  # Handle Pydantic validation errors
import shutil
import time
# local webserver dashboard will be run in a separate thread
from threading import Thread
import uvicorn
from web_interface.server import app

def start_web_interface():
    logger.info("Starting web interface on http://localhost:8000")
    # Run the FastAPI app with Uvicorn
    # Note: This will block the thread, so it should be run in a separate thread
    # If you want to run it in the main thread, remove the Thread wrapper
    # and call uvicorn.run directly.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")  # this is the blocking command
    # When the server stops, the line below will execute
    logger.info("Web interface has exited")

def setup_logging():
    """
    Set up logging configuration for the application.
    """
    # Create the LOGS subdirectory if it doesn't exist
    os.makedirs("LOGS", exist_ok=True)

    # Remove any default logging handlers
    logger.remove()
    # JSON log file with rotation and retention settings
    logger.add(
        "LOGS/vmc_{time:YYYY-MM-DD_HH-mm-ss}.log.json",
        serialize=True,
        rotation="00:00",
        retention="3 days",
        compression="zip"
    )
    # Add console logging for INFO and ERROR messages (plain text, with custom format)
    logger.add(
        sys.stdout,
        level="INFO",
        serialize=False,
        format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}\n"
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

@logger.catch()
def main():
    # configure logging
    setup_logging()

    logger.info("Starting Vending Machine Controller")

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

        json_text = merged_data.model_dump_json(indent=4)
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
            loc = " -> ".join(str(l) for l in err.get('loc', []))
            msg = err.get('msg', '')
            logger.error(f"  • {loc}: {msg}")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error validating configuration")
        sys.exit(1)

    # Start the web interface in a separate thread to avoid blocking the main thread
    logger.info("Starting web interface in a separate thread")
    Thread(target=start_web_interface, daemon=True).start()
    # Then start your FSM/main loop below

    logger.debug("Instantiating VendingMachineUI with configuration model")
    # TODO launch the vending machine FSM or main loop here

    """
    # Initialize Tkinter UI
    try:
        logger.debug("Initializing Tkinter root window and UI")
        root = tk.Tk()
        root.title("Vending Machine Controller")
        logger.debug("Instantiating VendingMachineUI with configuration model")
        app = VendingMachineUI(root, config_model=config_model)
    except Exception:
        logger.exception("Failed to initialize Tkinter UI")
        sys.exit(1)

    # Enter main loop
    try:
        logger.info("Entering Tkinter main loop")
        root.mainloop()
    except Exception:
        logger.exception("Error during Tkinter main loop")
    finally:
        logger.info("Tkinter main loop has exited")
    """


if __name__ == "__main__":
    main()
