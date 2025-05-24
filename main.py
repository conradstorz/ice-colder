import os
import sys
import tkinter as tk
from loguru import logger
from config.config_model import ConfigModel
from hardware.tkinter_ui import VendingMachineUI

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

@logger.catch()
def main():
    logger.info("Starting Vending Machine Controller")

    # Load and validate configuration using Pydantic ConfigModel
    try:
        logger.debug("Loading configuration from 'config.json' via Pydantic model")
        config_model = ConfigModel.model_validate_file(
            "config.json"
        )
        version = getattr(config_model, "version", None)
        logger.info(
            "Configuration loaded and validated successfully%s",
            f": version={version}" if version else ""
        )
    except FileNotFoundError:
        logger.error("Configuration file 'config.json' not found")
        sys.exit(1)
    except Exception:
        logger.exception("Failed to load or validate configuration file")
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
