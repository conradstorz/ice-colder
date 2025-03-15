# controller/vmc.py
import asyncio
from transitions import Machine
from loguru import logger

from hardware.coin_handler import CoinHandler
from services.payment import PaymentService

class VMC:
    # Define FSM states
    states = ['idle', 'accepting_payment', 'dispensing', 'error']

    def __init__(self):
        # Initialize the state machine
        self.machine = Machine(model=self, states=VMC.states, initial='idle')
        self.machine.add_transition(trigger='start_payment', source='idle', dest='accepting_payment', before='log_start_payment')
        self.machine.add_transition(trigger='dispense_product', source='accepting_payment', dest='dispensing', before='log_dispense')
        self.machine.add_transition(trigger='reset', source=['dispensing', 'error'], dest='idle', before='log_reset')
        self.machine.add_transition(trigger='error_occurred', source='*', dest='error', before='log_error')
        
        # Create instances of hardware and service mocks
        self.coin_handler = CoinHandler()
        self.payment_service = PaymentService()

    def log_start_payment(self):
        logger.info("Transitioning from idle to accepting_payment")

    def log_dispense(self):
        logger.info("Transitioning from accepting_payment to dispensing")

    def log_reset(self):
        logger.info("Resetting to idle state")

    def log_error(self):
        logger.error("Error encountered, transitioning to error state")

    async def run(self):
        logger.info("VMC running. Initial state: {}", self.state)
        while True:
            if self.state == 'idle':
                logger.debug("State idle: Waiting for customer interaction.")
                await asyncio.sleep(2)  # Simulate waiting period
                self.start_payment()

            elif self.state == 'accepting_payment':
                logger.debug("State accepting_payment: Processing input from hardware and payment service.")
                coin_result = self.coin_handler.insert_coin()
                if coin_result:
                    payment_result = self.payment_service.process_payment()
                    if payment_result:
                        self.dispense_product()
                    else:
                        self.error_occurred()
                else:
                    self.error_occurred()
                await asyncio.sleep(2)

            elif self.state == 'dispensing':
                logger.info("State dispensing: Dispensing product...")
                # Simulate product dispensing operation
                await asyncio.sleep(2)
                self.reset()

            elif self.state == 'error':
                logger.warning("State error: Handling error, notifying owner, and resetting.")
                # Insert error handling and notification logic here
                await asyncio.sleep(2)
                self.reset()

