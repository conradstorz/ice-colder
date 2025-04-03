# main.py
import os
import sys
import tkinter as tk
from loguru import logger
from tkinter_ui import VendingMachineUI

# Create the LOGS subdirectory if it doesn't exist
os.makedirs("LOGS", exist_ok=True)

# Remove any default logging handlers
logger.remove()
# Add a new logfile in the LOGS subdirectory with a timestamp in the filename,
# serialized as JSON, rotating at midnight, with logs older than 3 days compressed into a zip archive.
logger.add("LOGS/vmc_{time:YYYY-MM-DD_HH-mm-ss}.log", serialize=True, rotation="00:00", retention="3 days", compression="zip")
# Add console logging for INFO and ERROR messages without serialization (plain text)
logger.add(sys.stdout, level="INFO", serialize=False, format="{message}\n{level}: {time:YYYY-MM-DD HH:mm:ss}\n")

def main():
    logger.info("Starting Vending Machine Controller with Tkinter UI")
    root = tk.Tk()
    root.title("Vending Machine Controller")
    # the following line is not in error as Ruff has suggested
    app = VendingMachineUI(root)  # without this line there are no data in the tkinter window.
    root.mainloop()

if __name__ == "__main__":
    main()
