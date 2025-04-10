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
        logger.debug(f"Initializing {self.device_name} FSM.")

    def register_callback(self, callback):
        """
        Register a callback to send progress or status updates to the primary VMC.
        """
        self.callback = callback
        logger.debug(f"Callback registered for {self.device_name} FSM.")

    @abstractmethod
    def start_transaction(self):
        """Start the payment device transaction."""
        pass

    @abstractmethod
    def cancel_transaction(self):
        """Cancel the current transaction."""
        pass

    @abstractmethod
    def get_current_credit(self) -> float:
        """Return current credit value processed by the device."""
        pass

    @abstractmethod
    def dispense_change(self):
        """Dispense any change required for the transaction."""
        pass

    def notify(self, event_type, data):
        """
        Notify the primary VMC of an event.
        """
        logger.info(f"{self.device_name} FSM: Notifying event '{event_type}' with data: {data}")
        if self.callback:
            self.callback(event_type, data)
