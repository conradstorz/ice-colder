# controller/vmc.py
import asyncio
import json
from transitions import Machine
from loguru import logger

from hardware.coin_handler import CoinHandler
from hardware.button_panel import ButtonPanel
from services.payment import PaymentService

class VMC:
    # Define FSM states
    states = ['idle', 'accepting_payment', 'dispensing', 'error']

    def __init__(self, config_file='config.json'):
        # Load configuration from file
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        
        self.products = self.config.get("products", [])
        self.owner_contact = self.config.get("owner_contact", {})
        self.selected_product = None
        
        # Initialize the FSM
        self.machine = Machine(model=self, states=VMC.states, initial='idle')
        self.machine.add_transition(trigger='start_payment', source='idle', dest='accepting_payment', before='log_start_payment')
        self.machine.add_transition(trigger='dispense_product', source='accepting_payment', dest='dispensing', before='log_dispense')
        self.machine.add_transition(trigger='reset', source=['dispensing', 'error'], dest='idle', before='log_reset')
        self.machine.add_transition(trigger='error_occurred', source='*', dest='error', before='log_error')
        
        # Create instances of hardware and service mocks
        self.coin_handler = CoinHandler()
        self.button_panel = ButtonPanel()
        self.payment_service = PaymentService()

    def log_start_payment(self):
        logger.info(f"Transitioning from idle to accepting_payment for product: {self.selected_product}")

    def log_dispense(self):
        logger.info(f"Transitioning from accepting_payment to dispensing for product: {self.selected_product}")

    def log_reset(self):
        logger.info("Resetting to idle state. Clearing selected product.")
        self.selected_product = None

    def log_error(self):
        logger.error(f"Error encountered during operation for product: {self.selected_product}. Transitioning to error state.")

    async def run(self):
        logger.info(f"VMC running. Initial state: {self.state}")
        while True:
            if self.state == 'idle':
                logger.debug("State idle: Waiting for button press (simulated).")
                # Wait for a simulated button press (demon function)
                pressed_button = await self.button_panel.wait_for_press()
                if pressed_button < len(self.products):
                    self.selected_product = self.products[pressed_button]
                    logger.info(f"Product selected: {self.selected_product.get('name')} at price ${self.selected_product.get('price'):.2f}")
                    self.start_payment()
                else:
                    logger.error("Invalid button pressed. Remaining in idle.")
                    continue

            elif self.state == 'accepting_payment':
                logger.debug(f"State accepting_payment: Initiating payment for product: {self.selected_product.get('name')}")
                # Simulate payment process using product price
                price = self.selected_product.get("price", 0)
                payment_result = self.payment_service.process_payment(price)
                if payment_result:
                    self.dispense_product()
                else:
                    self.error_occurred()
                await asyncio.sleep(2)

            elif self.state == 'dispensing':
                logger.info(f"State dispensing: Dispensing product: {self.selected_product.get('name')}")
                # Simulate dispensing operation
                await asyncio.sleep(2)
                self.reset()

            elif self.state == 'error':
                logger.warning(f"State error: Handling error for product: {self.selected_product.get('name') if self.selected_product else 'None'}. Notifying owner: {self.owner_contact}")
                # Here, implement error handling and notification (email/SMS) as needed
                await asyncio.sleep(2)
                self.reset()
