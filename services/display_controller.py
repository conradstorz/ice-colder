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
        self._setup_code: str | None = None
        self._last_state: str = "idle"

    @property
    def current_mode(self) -> DisplayMode:
        return self._current_mode

    @property
    def setup_code(self) -> str | None:
        """The setup code currently held on the display, if any."""
        return self._setup_code

    def set_mqtt(self, client, loop):
        """Attach MQTT client and event loop for publishing commands."""
        self._mqtt_client = client
        self._loop = loop
        logger.debug("DisplayController: MQTT client attached.")

    def update_for_state(self, vmc_state: str):
        """
        Update the display mode based on the current VMC FSM state.
        Only publishes if the mode actually changes.

        While a setup code is held (`show_setup_code`), the state is still
        recorded (so `clear_setup_code` knows what to return to) but nothing
        is published — an FSM transition must not be able to wipe the setup
        code off the screen while someone is reading it at the machine.
        """
        self._last_state = vmc_state
        if self._setup_code is not None:
            return

        new_mode = _STATE_TO_MODE.get(vmc_state, DisplayMode.advertising)
        if new_mode == self._current_mode:
            return

        old_mode = self._current_mode
        self._current_mode = new_mode
        logger.info(
            f"Display: {old_mode.value} -> {new_mode.value} (state={vmc_state})"
        )
        self._publish_mode(new_mode)

    def set_mode(self, mode: DisplayMode):
        """
        Manually set the display mode (e.g., for maintenance).
        Always publishes, even if the mode hasn't changed.
        """
        self._current_mode = mode
        logger.info(f"Display: manually set to {mode.value}")
        self._publish_mode(mode)

    def show_setup_code(self, code: str) -> None:
        """
        Put the machine into setup mode: switch to maintenance and publish
        the setup code, split into two groups of four digits, for as long as
        setup mode lasts. Also logs the plaintext code at warning level, so
        it lands in the startup log alongside the display.
        """
        self._setup_code = code
        message = f"Setup code: {code[:4]} {code[4:]}"
        logger.warning(message)

        self._current_mode = DisplayMode.maintenance
        self._publish_mode(DisplayMode.maintenance, message=message)

    def clear_setup_code(self) -> None:
        """
        Forget the held setup code and republish the mode for the last
        recorded FSM state. A no-op (no publish) if no code is held.
        """
        if self._setup_code is None:
            return

        self._setup_code = None
        new_mode = _STATE_TO_MODE.get(self._last_state, DisplayMode.advertising)
        self._current_mode = new_mode
        logger.info(f"Display: setup code cleared, mode -> {new_mode.value}")
        self._publish_mode(new_mode)

    def _publish_mode(self, mode: DisplayMode, message: str | None = None):
        """Publish a DisplayCommand to MQTT."""
        if self._mqtt_client is None or self._loop is None:
            logger.debug(
                f"Display: mode={mode.value} (MQTT not connected, skipped publish)"
            )
            return

        command = DisplayCommand(mode=mode, message=message)
        self._loop.create_task(self._mqtt_client.publish("cmd/display", command))
