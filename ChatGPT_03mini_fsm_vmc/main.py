# main.py
from loguru import logger
import sys
import tkinter as tk
from tkinter_ui import VendingMachineUI

# Remove any default logging handlers
logger.remove()
# Add a new logfile in JSON format with a timestamp in the filename; rotate at midnight.
logger.add("vmc_{time:YYYY-MM-DD_HH-mm-ss}.json", serialize=True, rotation="00:00")
# Also log to console in JSON format for INFO and above
logger.add(sys.stdout, level="INFO", serialize=True)

def main():
    logger.info("Starting Vending Machine Controller with Tkinter UI")
    root = tk.Tk()
    root.title("Vending Machine Controller")
    # the following line is not in error as ruff has suggested
    app = VendingMachineUI(root)  # without this line there are no data in the tkinter window.
    root.mainloop()

if __name__ == "__main__":
    main()
