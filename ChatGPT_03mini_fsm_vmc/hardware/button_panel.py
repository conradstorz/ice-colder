# hardware/button_panel.py
import random
import asyncio
from loguru import logger

class ButtonPanel:
    def __init__(self):
        # Represent four buttons corresponding to the four products
        self.buttons = [0, 1, 2, 3]

    async def wait_for_press(self):
        logger.info("ButtonPanel: Waiting for button press (simulated delay: 3 minutes)...")
        await asyncio.sleep(180)  # 3 minutes delay; adjust for faster testing if needed
        pressed_button = random.choice(self.buttons)
        logger.info(f"ButtonPanel: Simulated button press: Button {pressed_button}")
        return pressed_button
