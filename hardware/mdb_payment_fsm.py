# mdb_payment_fsm.py
import asyncio

from loguru import logger

from services.async_payment_fsm import AsyncPaymentFSM


class MDBPaymentFSM(AsyncPaymentFSM):
    """
    Asynchronous FSM for handling MDB interface payment devices.
    Handles physical payment devices (cash, coin, credit card readers) via the MDB standard.
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

    async def refund(self, amount: float):
        """
        Refund part or all of the payment.
        In this simulation, the refund amount is deducted from the current_credit.
        """
        if amount <= 0:
            logger.error("MDBPaymentFSM: Refund amount must be positive.")
            raise ValueError("Refund amount must be positive.")
        if amount > self.current_credit:
            amount = self.current_credit  # Refund whatever is available
        self.current_credit -= amount
        logger.info(f"MDBPaymentFSM: Refunding ${amount:.2f}. Remaining credit: ${self.current_credit:.2f}")
        self.notify("refund_processed", {"device": "MDB", "refund_amount": amount})
        await asyncio.sleep(0.1)
        return amount
