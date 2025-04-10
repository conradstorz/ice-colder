# async_payment_fsm.py
import abc
from abc import ABC, abstractmethod
from loguru import logger

class AsyncPaymentFSM(ABC):
    """
    Abstract base class for asynchronous payment FSMs.
    Provides a common interface for both physical (MDB-based)
    and virtual payment systems.
    """
    def __init__(self, name: str, callback=None):
        self.name = name
        self.callback = callback
        logger.debug(f"{self.name} AsyncPaymentFSM initialized.")

    def register_callback(self, callback):
        self.callback = callback
        logger.debug(f"{self.name} AsyncPaymentFSM: Callback registered.")

    def notify(self, event_type, data):
        logger.info(f"{self.name} AsyncPaymentFSM: Notifying event '{event_type}' with data: {data}")
        if self.callback:
            self.callback(event_type, data)

    @abstractmethod
    async def start_transaction(self, *args, **kwargs):
        """Begin a payment transaction asynchronously."""
        pass

    @abstractmethod
    async def cancel_transaction(self):
        """Cancel an active transaction asynchronously."""
        pass

    @abstractmethod
    async def get_status(self) -> dict:
        """Return current status details as a dict asynchronously."""
        pass

    @abstractmethod
    async def dispense_change(self):
        """Dispense any required change asynchronously."""
        pass
