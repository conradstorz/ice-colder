"""
fsm_integration.py

This module integrates the external FSMs with the primary VMC.
It instantiates the MDB-based FSM (handling physical payment devices such as cash, coin, and credit card readers)
and the asynchronous Virtual Payment FSM (handling virtual payment providers).
Both FSMs report their events via a common callback so that the primary VMC can update its state and UI accordingly.
"""

import asyncio
from loguru import logger
from hardware.mdb_payment_fsm import MDBPaymentFSM
from services.virtual_payment_fsm import VirtualPaymentFSM

# -----------------------------------------------------------------------------
# Integration Callback Function
# -----------------------------------------------------------------------------
def vmc_callback(event_type: str, data: dict):
    """
    Callback function to handle events from FSMs.

    This is the primary notification mechanism from external FSMs back to the VMC.
    In a production environment, the VMC may use this callback to update its internal state,
    trigger UI updates, or perform other actions based on event types such as:
      - transaction_started
      - payment_success
      - payment_timeout
      - change_dispensed
      - transaction_cancelled
    """
    logger.info(f"VMC received event: {event_type} | data: {data}")
    # TODO: Integrate this callback with the primary VMC logic.
    # For example:
    #   if event_type == "transaction_started":
    #       vmc.set_state("processing_payment")
    #   elif event_type == "payment_success":
    #       vmc.set_state("dispensing")
    #   etc.
    pass

# -----------------------------------------------------------------------------
# FSM Integration Class
# -----------------------------------------------------------------------------
class FSMIntegration:
    """
    Integrates external FSMs with the primary VMC.

    This class initializes and manages the two key payment FSMs:
      - The MDBPaymentFSM for physical payment devices.
      - The VirtualPaymentFSM for virtual payment providers.

    It provides methods to start a physical payment transaction, initiate a virtual payment,
    and cancel any ongoing virtual payment tasks.
    """
    def __init__(self, payment_gateways=None, event_callback=vmc_callback):
        """
        Initialize the FSM Integration module.

        :param payment_gateways: A dictionary of virtual payment provider instances.
                                 Each provider must implement:
                                     - generate_payment_url(amount)
                                     - check_payment_status()
                                 Example:
                                     {
                                         "paypal": PayPalProvider(),
                                         "stripe": StripeProvider(),
                                     }
        :param event_callback: Callback function to forward events to the primary VMC.
        """
        if payment_gateways is None:
            payment_gateways = {}

        self.event_callback = event_callback

        # Initialize the MDB-based FSM for physical payments (which now handles coins, cash, and card readers)
        self.mdb_fsm = MDBPaymentFSM(callback=self.event_callback)
        logger.debug("FSMIntegration: Initialized MDBPaymentFSM.")

        # Initialize the Virtual Payment FSM if virtual payment providers are available
        self.payment_gateways = payment_gateways
        if self.payment_gateways:
            self.virtual_payment_fsm = VirtualPaymentFSM(self.payment_gateways, callback=self.event_callback)
            logger.debug("FSMIntegration: Initialized VirtualPaymentFSM with providers: "
                         f"{list(self.payment_gateways.keys())}.")
        else:
            self.virtual_payment_fsm = None
            logger.warning("FSMIntegration: No virtual payment providers configured.")

    def start_physical_payment(self):
        """
        Start the physical payment transaction via the MDBPaymentFSM.
        """
        logger.info("FSMIntegration: Starting physical payment transaction.")
        self.mdb_fsm.start_transaction()

    async def start_virtual_payment(self, amount: float):
        """
        Initiate a virtual payment transaction asynchronously.

        :param amount: The payment amount to process.
        :return: The name of the successful virtual payment provider if any, or None.
        """
        if not self.virtual_payment_fsm:
            logger.error("FSMIntegration: Virtual payment FSM is not initialized.")
            return None

        logger.info(f"FSMIntegration: Starting virtual payment for amount: ${amount:.2f}")
        result = await self.virtual_payment_fsm.start_virtual_payment(amount)
        return result

    def cancel_virtual_payment(self):
        """
        Cancel any ongoing virtual payment transactions.
        """
        if self.virtual_payment_fsm:
            logger.info("FSMIntegration: Cancelling virtual payment transaction.")
            self.virtual_payment_fsm.cancel_virtual_payment()
        else:
            logger.warning("FSMIntegration: Virtual payment FSM is not initialized.")
