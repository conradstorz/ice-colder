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
    # Define FSM states: idle, interacting_with_user, dispensing, error
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    def __init__(self, config_file="config.json"):
        # Load configuration
        with open(config_file, "r") as f:
            self.config = json.load(f)
        self.products = self.config.get("products", [])
        self.owner_contact = self.config.get("owner_contact", {})

        # Initialize business data
        self.selected_product = None
        self.credit_escrow = 0.0
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"  # Default payment method

        # Callbacks for UI updates
        self.update_callback = None  # Expected signature: (state, selected_product, credit_escrow)
        self.message_callback = None  # Expected signature: (message)

        # Setup FSM transitions using transitions library
        self.machine = Machine(model=self, states=VMC.states, initial="idle")
        self.machine.add_transition(
            trigger="start_interaction",
            source="idle",
            dest="interacting_with_user",
            before="on_start_interaction",
        )
        self.machine.add_transition(
            trigger="dispense_product",
            source="interacting_with_user",
            dest="dispensing",
            before="on_dispense_product",
        )
        self.machine.add_transition(
            trigger="complete_transaction",
            source="dispensing",
            dest="interacting_with_user",
            conditions="has_credit",
            before="on_complete_transaction",
        )
        self.machine.add_transition(
            trigger="complete_transaction",
            source="dispensing",
            dest="idle",
            unless="has_credit",
            before="on_complete_transaction",
        )
        self.machine.add_transition(
            trigger="error_occurred",
            source="*",
            dest="error",
            before="on_error",
        )
        self.machine.add_transition(
            trigger="reset_state",
            source=["error"],
            dest="idle",
            before="on_reset",
        )

        # Initialize hardware and services
        self.coin_handler = CoinHandler()         # Placeholder for future expansion
        self.payment_service = PaymentService()
        self.mdb_interface = MDBInterface()

    # --- Condition Methods ---
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        return self.credit_escrow > 0

    # --- Callback Setters ---
    def set_update_callback(self, callback):
        self.update_callback = callback

    def set_message_callback(self, callback):
        self.message_callback = callback

    # --- Unified Message Routine ---
    def send_customer_message(self, message, tk_root=None, duration=9000):
        """
        Send a message to the customer via the UI.
        All messages should go through this method to allow for centralized control of timing.
        If tk_root is provided, the message will be cleared after 'duration' milliseconds.
        """
        self._display_message(message)
        if tk_root is not None:
            tk_root.after(duration, lambda: self._display_message(""))

    # --- UI Helper Methods ---
    def _refresh_ui(self):
        if self.update_callback:
            self.update_callback(self.state, self.selected_product, self.credit_escrow)

    def _display_message(self, message):
        if self.message_callback:
            self.message_callback(message)

    # --- FSM Callback Methods with Enhanced Logging ---
    def on_start_interaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from idle to interacting_with_user for product: {self.selected_product}")
        self._refresh_ui()

    def on_dispense_product(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from interacting_with_user to dispensing for product: {self.selected_product}")
        self._refresh_ui()

    def on_complete_transaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Completing transaction. Remaining escrow: ${self.credit_escrow:.2f}")
        self._refresh_ui()

    def on_reset(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Resetting to idle state. Clearing selected product. Previous selection: {self.selected_product}")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._refresh_ui()
        # self.send_customer_message("", duration=0)  # No need to clear the display it happens automatically

    def on_error(self):
        logger.error(f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state.")
        self._refresh_ui()

    # --- Business Logic Methods ---
    def deposit_funds(self, amount, payment_method="Simulated Payment"):
        """Deposit funds into the escrow and record the payment method."""
        self.credit_escrow += amount
        self.last_payment_method = payment_method
        logger.info(f"Deposited ${amount:.2f} via {payment_method}. New escrow: ${self.credit_escrow:.2f}")
        self._refresh_ui()

    def request_refund(self, tk_root=None):
        """Process a refund for any unused credit in the escrow."""
        if self.credit_escrow > 0:
            refund_amount = self.credit_escrow
            self.credit_escrow = 0.0
            logger.info(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.")
            self.send_customer_message(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.", tk_root)
            self._refresh_ui()
        else:
            self.send_customer_message("No funds to refund.", tk_root)

    def select_product(self, product_index, tk_root):
        """
        Called by the UI when a product button is pressed.
        Works in both 'idle' and 'interacting_with_user' states.
        """
        if self.state not in ["idle", "interacting_with_user"]:
            logger.warning("Cannot change selection; machine not ready.")
            return
        if product_index >= len(self.products):
            logger.error("Invalid product index selected.")
            return

        self.selected_product = self.products[product_index]
        logger.info(f"Selected product: {self.selected_product.get('name')} at ${self.selected_product.get('price'):.2f}")

        # Check if the product is tracked and sold out
        if self.selected_product.get("track_inventory", False):
            if self.selected_product.get("inventory_count", 0) <= 0:
                logger.error(f"{self.selected_product.get('name')} is sold out.")
                self.send_customer_message(f"{self.selected_product.get('name')} is sold out. Please select another product.", tk_root)
                return

        if self.state == "idle":
            self.start_interaction()
            tk_root.after(1000, lambda: self._process_payment(tk_root))
        elif self.state == "interacting_with_user":
            self._update_selection_message(tk_root)
            tk_root.after(1000, lambda: self._process_payment(tk_root))
        self._refresh_ui()

    def _update_selection_message(self, tk_root):
        """Update message when product selection changes in interacting_with_user state."""
        price = self.selected_product.get("price", 0)
        if self.credit_escrow < price:
            required = price - self.credit_escrow
            message = f"Changed selection to {self.selected_product.get('name')}. Insert additional ${required:.2f}."
        else:
            message = f"Changed selection to {self.selected_product.get('name')}. Sufficient funds available."
        self.send_customer_message(message, tk_root)
        self.last_insufficient_message = message

    def _process_payment(self, tk_root):
        """Process payment by checking funds and scheduling next steps."""
        if self.state != "interacting_with_user":
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
                self.send_customer_message(message, tk_root)
                self.last_insufficient_message = message
            tk_root.after(5000, lambda: self._process_payment(tk_root))

    def _finish_dispensing(self, tk_root):
        """Finalize dispensing and transition state based on remaining credit."""
        if self.state != "dispensing":
            return
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {self.selected_product.get('name')}")
        # If product inventory is tracked, decrement the count
        if self.selected_product.get("track_inventory", False):
            self.selected_product["inventory_count"] -= 1
            logger.info(f"Inventory for {self.selected_product.get('name')} updated: {self.selected_product['inventory_count']} remaining.")
        # Complete the transaction: if unused credit remains, transition to interacting_with_user; otherwise, reset to idle.
        self.complete_transaction()
        self._refresh_ui()

    async def start_mdb_monitoring(self):
        """Start asynchronous monitoring of the MDB bus."""
        await self.mdb_interface.read_messages(self.handle_mdb_message)

    def handle_mdb_message(self, message):
        """Stub: Handle incoming MDB messages."""
        logger.info(f"Received MDB message: {message}")
