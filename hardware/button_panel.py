# hardware/button_panel.py
import asyncio
import random

from loguru import logger


class ButtonPanel:
    def __init__(self, num_buttons: int):
        # Create a list of button indices based on the number of products
        self.buttons = list(range(num_buttons))

    async def wait_for_press(self):
        delay = random.uniform(0.001, 0.01)
        logger.info(
            f"ButtonPanel: Waiting for button press (simulated delay: {delay:.2f} seconds)..."
        )
        await asyncio.sleep(delay)
        pressed_button = random.choice(self.buttons)
        logger.info(f"ButtonPanel: Simulated button press: Button {pressed_button}")
        return pressed_button
