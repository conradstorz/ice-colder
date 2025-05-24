import os
import sys
import tkinter as tk
from loguru import logger
from config.config_model import ConfigModel
from hardware.tkinter_ui import VendingMachineUI
import json  # For loading configuration
from pydantic import ValidationError  # Handle Pydantic validation errors

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

@logger.catch()
def main():
    logger.info("Starting Vending Machine Controller")

    # Load and validate configuration using Pydantic ConfigModel
    try:
        logger.debug("Reading raw JSON from 'config.json'")
        with open("config.json", encoding="utf-8") as f:
            raw_json = f.read()

        logger.debug("Parsing and validating configuration via Pydantic model")
        config_model = ConfigModel.model_validate_json(raw_json)
        version = getattr(config_model, "version", None)
        logger.info(
            "Configuration loaded successfully%s",
            f": version={version}" if version else ""
        )

    except FileNotFoundError:
        logger.error("Configuration file 'config.json' not found")
        sys.exit(1)
    except ValidationError as ve:
        # Pydantic validation errors
        logger.error("Configuration validation failed with the following errors:")
        for err in ve.errors():
            loc = " -> ".join(str(l) for l in err.get('loc', []))
            msg = err.get('msg', '')
            logger.error(f"  • {loc}: {msg}")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error loading or validating configuration")
        sys.exit(1)

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

if __name__ == "__main__":
    main()
