# main.py
import tkinter as tk
from loguru import logger
from tkinter_ui import VendingMachineUI

# Configure loguru: Rotate log file at midnight
logger.add("vmc.log", rotation="00:00")


def main():
    logger.info(f"Starting Vending Machine Controller with Tkinter UI")
    root = tk.Tk()
    root.title("Vending Machine Controller")
    # the following line is not in error as ruff has suggested
    app = VendingMachineUI(root)  # without this line there are no data in the tkinter window.
    root.mainloop()


if __name__ == "__main__":
    main()
