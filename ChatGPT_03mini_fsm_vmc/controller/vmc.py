# controller/vmc.py
import json
from transitions import Machine
from loguru import logger
from services.payment import PaymentService
from hardware.coin_handler import CoinHandler
from hardware.mdb_interface import MDBInterface

# Global variable for state change log prefix
STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

class VMC:
    # Define FSM states
    states = ['idle', 'accepting_payment', 'dispensing', 'error']

    def __init__(self, config_file='config.json'):
        # Load configuration
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        self.products = self.config.get("products", [])
        self.owner_contact = self.config.get("owner_contact", {})

        # Initialize business data
        self.selected_product = None
        self.credit_escrow = 0.0
        self.last_insufficient_message = ""

        # Callbacks for UI updates
        self.update_callback = None  # Expected signature: (state, selected_product, credit_escrow)
        self.message_callback = None  # Expected signature: (message)

        # Setup FSM transitions using transitions library
        self.machine = Machine(model=self, states=VMC.states, initial='idle')
        self.machine.add_transition(trigger='start_payment', source='idle', dest='accepting_payment', before='on_start_payment')
        self.machine.add_transition(trigger='dispense_product', source='accepting_payment', dest='dispensing', before='on_dispense_product')
        self.machine.add_transition(trigger='reset_state', source=['dispensing', 'error'], dest='idle', before='on_reset')
        self.machine.add_transition(trigger='error_occurred', source='*', dest='error', before='on_error')

        # Initialize hardware and services
        self.coin_handler = CoinHandler()         # Placeholder for future expansion
        self.payment_service = PaymentService()
        self.mdb_interface = MDBInterface()

    # --- Callback Setters ---
    def set_update_callback(self, callback):
        self.update_callback = callback

    def set_message_callback(self, callback):
        self.message_callback = callback

    # --- UI Helper Methods ---
    def _refresh_ui(self):
        if self.update_callback:
            self.update_callback(self.state, self.selected_product, self.credit_escrow)

    def _display_message(self, message):
        if self.message_callback:
            self.message_callback(message)

    # --- FSM Callback Methods with Enhanced Logging ---
    def on_start_payment(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from idle to accepting_payment for product: {self.selected_product}")
        self._refresh_ui()

    def on_dispense_product(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from accepting_payment to dispensing for product: {self.selected_product}")
        self._refresh_ui()

    def on_reset(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Resetting to idle state. Clearing selected product. Previous selection: {self.selected_product}")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._refresh_ui()
        self._display_message("")

    def on_error(self):
        logger.error(f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state.")
        self._refresh_ui()

    # --- Business Logic Methods ---
    def deposit_funds(self, amount):
        """Deposit funds into the escrow."""
        self.credit_escrow += amount
        logger.info(f"Deposited ${amount:.2f}. New escrow: ${self.credit_escrow:.2f}")
        self._refresh_ui()

    def select_product(self, product_index, tk_root):
        """
        Called by the UI when a product button is pressed.
        Works in both 'idle' and 'accepting_payment' states.
        """
        if self.state not in ['idle', 'accepting_payment']:
            logger.warning("Cannot change selection; machine not ready.")
            return
        if product_index >= len(self.products):
            logger.error("Invalid product index selected.")
            return

        self.selected_product = self.products[product_index]
        logger.info(f"Selected product: {self.selected_product.get('name')} at ${self.selected_product.get('price'):.2f}")

        if self.state == 'idle':
            self.start_payment()
            tk_root.after(1000, lambda: self._process_payment(tk_root))
        else:
            self._update_selection_message()
        self._refresh_ui()

    def _update_selection_message(self):
        """Update message when product selection changes in accepting_payment state."""
        price = self.selected_product.get("price", 0)
        if self.credit_escrow < price:
            required = price - self.credit_escrow
            message = f"Changed selection to {self.selected_product.get('name')}. Insert additional ${required:.2f}."
        else:
            message = f"Changed selection to {self.selected_product.get('name')}. Sufficient funds available."
        self._display_message(message)
        self.last_insufficient_message = message

    def _process_payment(self, tk_root):
        """Process payment by checking funds and scheduling next steps."""
        if self.state != 'accepting_payment':
            return

        price = self.selected_product.get("price", 0)
        if self.credit_escrow >= price:
            logger.info(f"{STATE_CHANGE_PREFIX} Escrow sufficient (${self.credit_escrow:.2f} >= ${price:.2f}). Processing payment.")
            self.credit_escrow -= price
            self.dispense_product()
            self._refresh_ui()
            tk_root.after(1000, lambda: self._finish_dispensing(tk_root))
            self.last_insufficient_message = ""
        else:
            required = price - self.credit_escrow
            message = f"Insufficient funds. Please insert an additional ${required:.2f}."
            if message != self.last_insufficient_message:
                logger.error(message)
                self._display_message(message)
                self.last_insufficient_message = message
            tk_root.after(5000, lambda: self._process_payment(tk_root))

    def _finish_dispensing(self, tk_root):
        """Finalize dispensing and reset state."""
        if self.state != 'dispensing':
            return
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {self.selected_product.get('name')}")
        self.reset_state()
        self._refresh_ui()

    async def start_mdb_monitoring(self):
        """Start asynchronous monitoring of the MDB bus."""
        await self.mdb_interface.read_messages(self.handle_mdb_message)

    def handle_mdb_message(self, message):
        """Stub: Handle incoming MDB messages."""
        logger.info(f"Received MDB message: {message}")
