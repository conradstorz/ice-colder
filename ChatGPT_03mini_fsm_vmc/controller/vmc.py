# controller/vmc.py
import json
from transitions import Machine
from loguru import logger
from services.payment import PaymentService
from hardware.coin_handler import CoinHandler

# Import the MDB interface (ensure that hardware/mdb_interface.py is in your project)
from hardware.mdb_interface import MDBInterface

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
        self.update_callback = None  # Callback for UI updates

        # Initialize the FSM
        self.machine = Machine(model=self, states=VMC.states, initial='idle')
        self.machine.add_transition(trigger='start_payment', source='idle', dest='accepting_payment', before='log_start_payment')
        self.machine.add_transition(trigger='dispense_product', source='accepting_payment', dest='dispensing', before='log_dispense')
        self.machine.add_transition(trigger='reset', source=['dispensing', 'error'], dest='idle', before='log_reset')
        self.machine.add_transition(trigger='error_occurred', source='*', dest='error', before='log_error')

        # Create instances of hardware/service mocks
        self.coin_handler = CoinHandler()  # Currently available for future expansion
        self.payment_service = PaymentService()
        # Instantiate the MDB interface for near real-time monitoring
        self.mdb_interface = MDBInterface()

    def set_update_callback(self, callback):
        """Set a callback to update the UI with the current state and selected product."""
        self.update_callback = callback

    def _update_ui(self):
        if self.update_callback:
            self.update_callback(self.state, self.selected_product)

    def log_start_payment(self):
        logger.info(f"Transitioning from idle to accepting_payment for product: {self.selected_product}")
        self._update_ui()

    def log_dispense(self):
        logger.info(f"Transitioning from accepting_payment to dispensing for product: {self.selected_product}")
        self._update_ui()

    def log_reset(self):
        logger.info("Resetting to idle state. Clearing selected product.")
        self.selected_product = None
        self._update_ui()

    def log_error(self):
        logger.error(f"Error encountered during operation for product: {self.selected_product}. Transitioning to error state.")
        self._update_ui()

    def select_product(self, product_index, tk_root):
        """
        Triggered by the UI when a product button is pressed.
        Schedules the payment processing using tk_root.after().
        """
        if self.state != 'idle':
            logger.warning("Machine is not in idle state; cannot select product.")
            return
        if product_index >= len(self.products):
            logger.error("Invalid product index selected.")
            return

        self.selected_product = self.products[product_index]
        logger.info(f"Product selected: {self.selected_product.get('name')} at price ${self.selected_product.get('price'):.2f}")
        self.start_payment()
        self._update_ui()

        # Schedule payment processing after 1 second
        tk_root.after(1000, lambda: self.process_payment(tk_root))

    def process_payment(self, tk_root):
        if self.state != 'accepting_payment':
            return
        price = self.selected_product.get("price", 0)
        payment_result = self.payment_service.process_payment(price)
        if payment_result:
            self.dispense_product()
            self._update_ui()
            # Schedule dispensing to finish after 1 second
            tk_root.after(1000, lambda: self.finish_dispensing(tk_root))
        else:
            self.error_occurred()
            self._update_ui()
            # Schedule a reset after 1 second in case of error
            tk_root.after(1000, lambda: self.reset())

    def finish_dispensing(self, tk_root):
        if self.state != 'dispensing':
            return
        logger.info(f"Dispensing product: {self.selected_product.get('name')}")
        self.reset()
        self._update_ui()

    async def start_mdb_monitoring(self):
        """
        Start the asynchronous loop to monitor the MDB bus.
        The message_handler callback routes messages to appropriate FSM actions.
        """
        await self.mdb_interface.read_messages(self.handle_mdb_message)

    def handle_mdb_message(self, message):
        """
        Handle incoming messages from the MDB bus.
        Depending on the message, you might trigger FSM transitions or update internal state.
        This is a stub for where you'd decode the MDB protocol and act accordingly.
        """
        logger.info(f"VMC received MDB message: {message}")
        # Here you can parse the message and, for example, update coin escrow or trigger a transition.
