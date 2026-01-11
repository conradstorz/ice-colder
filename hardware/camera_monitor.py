# hardware/camera_monitor.py
import asyncio
import datetime
import os
import random

import cv2
from loguru import logger


class CameraMonitor:
    def __init__(self, camera_index: int = 0, output_dir: str = "customer_images"):
        self.camera_index = camera_index
        self.cap = cv2.VideoCapture(camera_index)
        self.output_dir = output_dir
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        logger.info(
            f"CameraMonitor: Initialized with camera index {camera_index}, output directory: {output_dir}"
        )

    async def monitor_customers(self):
        """
        Asynchronously monitor the security camera for customer presence.
        This stub function simulates a customer approaching by waiting a random interval
        between 3 and 10 seconds, then capturing a still image from the camera.
        The image is saved with a timestamped filename for later matching with other machine records.
        """
        while True:
            # Simulate a customer approach with a random delay
            delay = random.uniform(3, 10)
            logger.debug(
                f"CameraMonitor: Waiting {delay:.2f} seconds before capturing an image."
            )
            await asyncio.sleep(delay)

            ret, frame = self.cap.read()
            if ret:
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = os.path.join(self.output_dir, f"customer_{timestamp}.png")
                cv2.imwrite(filename, frame)
                logger.info(f"CameraMonitor: Captured customer image: {filename}")
            else:
                logger.error("CameraMonitor: Failed to capture image from camera.")
