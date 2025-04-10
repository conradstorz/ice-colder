# main.py
"""config file layout notes:
machine:
    Details:
        Name:
        Products:
        Physical Specs:
        Virtual Payment Providers:
        Maintenance Records:
        etc...
    Owner:
        Name:
        Contact:
        etc...
    Loation:
        Address:
        Description
        Contact:
        GPS:
        etc...
    Repair Service:
        Name:
        Contact:
        etc...
"""
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
logger.add("LOGS/vmc_{time:YYYY-MM-DD_HH-mm-ss}.log", serialize=True, rotation="00:00", retention="3 days", compression="zip")
# Add console logging for INFO and ERROR messages (plain text, with custom format)
logger.add(sys.stdout, level="INFO", serialize=False, format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}\n")

def main():
    logger.info("Starting Vending Machine Controller with Tkinter UI")
    # Read the configuration file manually (since parse_file is deprecated)
    with open("config.json", "r", encoding="utf8") as f:
        config_json = f.read()
    # Validate and parse the JSON using the Pydantic model
    config_model = ConfigModel.model_validate_json(config_json)
    
    root = tk.Tk()
    root.title("Vending Machine Controller")
    # Pass the validated configuration to the UI
    app = VendingMachineUI(root, config=config_model)
    root.mainloop()

if __name__ == "__main__":
    main()
