import os
import sys
import tkinter as tk
from pathlib import Path
from loguru import logger
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

    # Load configuration file
    json_config_path = (Path("config") / "config.json").resolve(strict=False)
    try:
        logger.debug(f"Attempting to open configuration file '{str(json_config_path)}'")
        with open(json_config_path, "r", encoding="utf8") as f:
            config_json = f.read()
        logger.debug(f"Configuration file read successfully ({len(config_json)} bytes)")
    except FileNotFoundError:
        logger.error(f"Configuration file '{str(json_config_path)}' not found")
        sys.exit(1)
    except Exception:
        logger.exception("Error reading configuration file")
        sys.exit(1)

    # Initialize Tkinter UI
    try:
        logger.debug("Initializing Tkinter root window")
        root = tk.Tk()
        root.title("Vending Machine Controller")

        logger.debug("Instantiating VendingMachineUI with configuration model")
        app = VendingMachineUI(root)
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
