# fsm_integration.py (excerpt)
from loguru import logger
from virtual_payment_fsm import VirtualPaymentFSM

from hardware.mdb_payment_fsm import MDBPaymentFSM


def vmc_callback(event_type: str, data: dict):
    """
    Central callback to handle events from both FSMs.
    In production, this would update VMC state, trigger UI updates, etc.
    """
    logger.info(f"VMC received event: {event_type} | data: {data}")
    # TODO: Handle events from FSMs accordingly.
    pass


class FSMIntegration:
    """
    Integrates the asynchronous MDB-based FSM and Virtual Payment FSM with the VMC.
    """

    def __init__(self, payment_gateways=None, event_callback=vmc_callback):
        if payment_gateways is None:
            payment_gateways = {}
        self.event_callback = event_callback

        # Initialize the MDB-based (physical) FSM.
        self.mdb_fsm = MDBPaymentFSM(callback=self.event_callback)
        logger.debug("FSMIntegration: Initialized MDBPaymentFSM.")

        # Initialize the Virtual Payment FSM if providers are available.
        self.payment_gateways = payment_gateways
        if self.payment_gateways:
            self.virtual_payment_fsm = VirtualPaymentFSM(self.payment_gateways, callback=self.event_callback)
            logger.debug(
                f"FSMIntegration: Initialized VirtualPaymentFSM with providers: {list(self.payment_gateways.keys())}."
            )
        else:
            self.virtual_payment_fsm = None
            logger.warning("FSMIntegration: No virtual payment providers configured.")

    async def start_physical_payment(self):
        logger.info("FSMIntegration: Starting physical payment transaction.")
        await self.mdb_fsm.start_transaction()

    async def start_virtual_payment(self, amount: float):
        if not self.virtual_payment_fsm:
            logger.error("FSMIntegration: Virtual payment FSM is not initialized.")
            return None
        logger.info(f"FSMIntegration: Starting virtual payment for amount: ${amount:.2f}")
        result = await self.virtual_payment_fsm.start_transaction(amount)
        return result

    async def cancel_virtual_payment(self):
        if self.virtual_payment_fsm:
            logger.info("FSMIntegration: Cancelling virtual payment transaction.")
            await self.virtual_payment_fsm.cancel_transaction()
        else:
            logger.warning("FSMIntegration: Virtual payment FSM is not initialized.")

    async def refund_physical_payment(self, amount: float):
        logger.info(f"FSMIntegration: Refunding physical payment: ${amount:.2f}")
        return await self.mdb_fsm.refund(amount)

    async def refund_virtual_payment(self, amount: float):
        if not self.virtual_payment_fsm:
            logger.error("FSMIntegration: Virtual payment FSM is not initialized.")
            return None
        logger.info(f"FSMIntegration: Refunding virtual payment: ${amount:.2f}")
        return await self.virtual_payment_fsm.refund(amount)
