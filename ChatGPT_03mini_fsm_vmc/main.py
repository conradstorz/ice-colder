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
    app = VendingMachineUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()

