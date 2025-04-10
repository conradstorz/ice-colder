# mdb_payment_fsm.py
import asyncio
from controller.payment_device_baseclass_fsm import PaymentDeviceFSM
from loguru import logger
from services.async_payment_fsm import AsyncPaymentFSM

class MDBPaymentFSM(AsyncPaymentFSM):
    """
    Asynchronous FSM for handling MDB interface payment devices.
    This FSM handles physical payment devices (coins, cash, credit card readers)
    via the MDB standard.
    """
    def __init__(self, callback=None):
        super().__init__("MDBPaymentFSM", callback=callback)
        self.current_credit = 0.0

    async def start_transaction(self):
        logger.info("MDBPaymentFSM: Starting MDB transaction.")
        self.current_credit = 0.0
        self.notify("transaction_started", {"device": "MDB"})
        # Simulate a short asynchronous initialization
        await asyncio.sleep(0.1)

    async def cancel_transaction(self):
        logger.info("MDBPaymentFSM: Cancelling MDB transaction.")
        # Implement cancellation logic here.
        self.notify("transaction_cancelled", {"device": "MDB"})
        await asyncio.sleep(0.1)

    async def get_status(self) -> dict:
        status = {"current_credit": self.current_credit}
        logger.debug(f"MDBPaymentFSM: Returning status: {status}")
        return status

    async def dispense_change(self):
        if self.current_credit > 0:
            logger.info(f"MDBPaymentFSM: Dispensing change: ${self.current_credit:.2f}")
            change = self.current_credit
            self.current_credit = 0.0
            self.notify("change_dispensed", {"device": "MDB", "amount": change})
        else:
            logger.debug("MDBPaymentFSM: No change to dispense.")
        await asyncio.sleep(0.1)
