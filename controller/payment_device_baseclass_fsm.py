# payment_device_fsm.py
from abc import ABC, abstractmethod
from loguru import logger

class PaymentDeviceFSM(ABC):
    """
    Abstract base class for Payment Device FSMs.
    This class defines the common interface and behavior for FSMs managing physical payment devices.
    """
    def __init__(self, device_name, callback=None):
        self.device_name = device_name
        self.callback = callback  # Expected to be a function with signature: callback(event_type: str, data: dict)
        logger.debug(f"{self.device_name} FSM: __init__ called. Callback set: {bool(callback)}")

    def register_callback(self, callback):
        """
        Register a callback to send progress or status updates to the primary VMC.
        """
        self.callback = callback
        logger.debug(f"{self.device_name} FSM: register_callback called. Callback updated.")

    @abstractmethod
    def start_transaction(self):
        """Start the payment device transaction."""
        logger.debug(f"{self.device_name} FSM: start_transaction invoked.")
        raise NotImplementedError("start_transaction must be implemented by subclass")

    @abstractmethod
    def cancel_transaction(self):
        """Cancel the current transaction."""
        logger.debug(f"{self.device_name} FSM: cancel_transaction invoked.")
        raise NotImplementedError("cancel_transaction must be implemented by subclass")

    @abstractmethod
    def get_current_credit(self) -> float:
        """Return current credit value processed by the device."""
        logger.debug(f"{self.device_name} FSM: get_current_credit invoked.")
        raise NotImplementedError("get_current_credit must be implemented by subclass")

    @abstractmethod
    def dispense_change(self):
        """Dispense any change required for the transaction."""
        logger.debug(f"{self.device_name} FSM: dispense_change invoked.")
        raise NotImplementedError("dispense_change must be implemented by subclass")

    def notify(self, event_type, data):
        """
        Notify the primary VMC of an event.
        """
        logger.debug(f"{self.device_name} FSM: notify called with event_type='{event_type}', data={data}")
        logger.info(f"{self.device_name} FSM: Notifying event '{event_type}' with data: {data}")
        if self.callback:
            logger.debug(f"{self.device_name} FSM: Executing callback for event '{event_type}'.")
            try:
                self.callback(event_type, data)
                logger.debug(f"{self.device_name} FSM: Callback executed successfully for event '{event_type}'.")
            except Exception as e:
                logger.exception(f"{self.device_name} FSM: Exception in callback for event '{event_type}': {e}")
        else:
            logger.warning(f"{self.device_name} FSM: No callback registered; event '{event_type}' not delivered.")
