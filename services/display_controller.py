# services/display_controller.py
"""
Customer-facing display controller.

Manages the display mode (advertising, transaction, error, maintenance)
and publishes mode-change commands to the ESP32 display controller via MQTT.

The RPi decides *what* to show; the ESP32 decides *how* to render it.
"""
from loguru import logger

from services.mqtt_messages import DisplayMode, DisplayCommand

# Maps VMC FSM states to display modes
_STATE_TO_MODE: dict[str, DisplayMode] = {
    "idle": DisplayMode.advertising,
    "interacting_with_user": DisplayMode.transaction,
    "dispensing": DisplayMode.transaction,
    "error": DisplayMode.error,
}


class DisplayController:
    """
    Tracks the current display mode and publishes changes via MQTT.

    Usage:
        display = DisplayController()
        display.set_mqtt(mqtt_client, loop)
        display.update_for_state("idle")       # -> advertising
        display.update_for_state("dispensing")  # -> transaction
    """

    def __init__(self):
        self._current_mode: DisplayMode = DisplayMode.advertising
        self._mqtt_client = None
        self._loop = None

    @property
    def current_mode(self) -> DisplayMode:
        return self._current_mode

    def set_mqtt(self, client, loop):
        """Attach MQTT client and event loop for publishing commands."""
        self._mqtt_client = client
        self._loop = loop
        logger.debug("DisplayController: MQTT client attached.")

    def update_for_state(self, vmc_state: str):
        """
        Update the display mode based on the current VMC FSM state.
        Only publishes if the mode actually changes.
        """
        new_mode = _STATE_TO_MODE.get(vmc_state, DisplayMode.advertising)
        if new_mode == self._current_mode:
            return

        old_mode = self._current_mode
        self._current_mode = new_mode
        logger.info(f"Display: {old_mode.value} -> {new_mode.value} (state={vmc_state})")
        self._publish_mode(new_mode)

    def set_mode(self, mode: DisplayMode):
        """
        Manually set the display mode (e.g., for maintenance).
        Always publishes, even if the mode hasn't changed.
        """
        self._current_mode = mode
        logger.info(f"Display: manually set to {mode.value}")
        self._publish_mode(mode)

    def _publish_mode(self, mode: DisplayMode):
        """Publish a DisplayCommand to MQTT."""
        if self._mqtt_client is None or self._loop is None:
            logger.debug(f"Display: mode={mode.value} (MQTT not connected, skipped publish)")
            return

        command = DisplayCommand(mode=mode)
        self._loop.create_task(self._mqtt_client.publish("cmd/display", command))
