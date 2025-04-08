# main.py
import os
import sys
import tkinter as tk
from loguru import logger
from config_model import ConfigModel
from tkinter_ui import VendingMachineUI

# Create the LOGS subdirectory if it doesn't exist
os.makedirs("LOGS", exist_ok=True)

logger.remove()
logger.add("LOGS/vmc_{time:YYYY-MM-DD_HH-mm-ss}.log", serialize=True, rotation="00:00", retention="3 days", compression="zip")
logger.add(sys.stdout, level="INFO", serialize=False, format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}\n")

def main():
    logger.info("Starting Vending Machine Controller with Tkinter UI")
    # Load configuration using Pydantic
    config_model = ConfigModel.parse_file("config.json")
    root = tk.Tk()
    root.title("Vending Machine Controller")
    app = VendingMachineUI(root, config=config_model)
    root.mainloop()

if __name__ == "__main__":
    main()
