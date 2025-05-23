import os
import sys
import tkinter as tk
from loguru import logger
from config_model import ConfigModel
from tkinter_ui import VendingMachineUI

# Create the LOGS subdirectory if it doesn't exist
os.makedirs("LOGS", exist_ok=True)

# Remove any default logging handlers
logger.remove()
# Add a new logfile in the LOGS subdirectory with a timestamp in the filename,
# serialized as JSON; rotate at midnight, retain logs for 3 days, and compress old logs.
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

def main():
    logger.info("Starting Vending Machine Controller with Tkinter UI")

    # Load configuration file
    try:
        logger.debug("Attempting to open configuration file 'config.json'")
        with open("config.json", "r", encoding="utf8") as f:
            config_json = f.read()
        logger.debug("Configuration file read successfully ({} bytes)", len(config_json))
    except FileNotFoundError:
        logger.error("Configuration file 'config.json' not found")
        sys.exit(1)
    except Exception:
        logger.exception("Error reading configuration file")
        sys.exit(1)

    # Validate configuration JSON
    try:
        logger.debug("Validating configuration JSON with Pydantic model")
        config_model = ConfigModel.model_validate_json(config_json)
        logger.info(
            "Configuration validated successfully: version={} ",
            getattr(config_model, "version", "N/A")
        )
    except Exception:
        logger.exception("Configuration validation failed")
        sys.exit(1)

    # Initialize Tkinter UI
    try:
        logger.debug("Initializing Tkinter root window")
        root = tk.Tk()
        root.title("Vending Machine Controller")

        logger.debug("Instantiating VendingMachineUI with configuration model")
        app = VendingMachineUI(root, config_model=config_model)
    except Exception:
        logger.exception("Failed to initialize UI")
        sys.exit(1)

    # Start main event loop
    try:
        logger.info("Entering Tkinter main loop")
        root.mainloop()
    except Exception:
        logger.exception("Error in Tkinter main loop")
    finally:
        logger.info("Tkinter main loop has exited")


if __name__ == "__main__":
    main()
