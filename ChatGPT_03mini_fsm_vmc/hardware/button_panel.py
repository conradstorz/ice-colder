# hardware/button_panel.py
import random
import asyncio
from loguru import logger

class ButtonPanel:
    def __init__(self, num_buttons: int):
        # Create a list of button indices based on the number of products
        self.buttons = list(range(num_buttons))

    async def wait_for_press(self):
        logger.info(f"ButtonPanel: Waiting for button press (simulated delay: 3 minutes)...")
        await asyncio.sleep(180)  # 3 minutes delay; adjust for testing if needed
        pressed_button = random.choice(self.buttons)
        logger.info(f"ButtonPanel: Simulated button press: Button {pressed_button}")
        return pressed_button
