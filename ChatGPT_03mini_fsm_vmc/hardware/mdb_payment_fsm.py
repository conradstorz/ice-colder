# mdb_payment_fsm.py
from payment_device_baseclass_fsm import PaymentDeviceFSM
from loguru import logger

class MDBPaymentFSM(PaymentDeviceFSM):
    """
    FSM for handling MDB interface payment devices.
    This FSM manages interactions with devices that adhere to the MDB standard.
    """
    def __init__(self, callback=None):
        super().__init__("MDB", callback=callback)
        self.current_credit = 0.0

    def start_transaction(self):
        logger.info("MDBPaymentFSM: Starting MDB transaction.")
        self.current_credit = 0.0
        self.notify("transaction_started", {"device": "MDB"})

    def cancel_transaction(self):
        logger.info("MDBPaymentFSM: Cancelling MDB transaction.")
        self.notify("transaction_cancelled", {"device": "MDB"})

    def get_current_credit(self) -> float:
        logger.debug(f"MDBPaymentFSM: Current credit is {self.current_credit:.2f}")
        return self.current_credit

    def dispense_change(self):
        if self.current_credit > 0:
            logger.info(f"MDBPaymentFSM: Dispensing change: ${self.current_credit:.2f}")
            change = self.current_credit
            self.current_credit = 0.0
            self.notify("change_dispensed", {"device": "MDB", "amount": change})
        else:
            logger.debug("MDBPaymentFSM: No change to dispense.")
