# controller/vmc.py
import json
from transitions import Machine
from loguru import logger
from services.payment_gateway_manager import PaymentGatewayManager
from hardware.coin_handler import CoinHandler
from hardware.mdb_interface import MDBInterface

# Global variable for state change log prefix
STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

class VMC:
    # Define FSM states: idle, interacting_with_user, dispensing, error
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    def __init__(self, config_file="config.json"):
        logger.debug(f"Initializing VMC with configuration file: {config_file}")
        # Load configuration
        with open(config_file, "r") as f:
            self.config = json.load(f)
        logger.debug(f"Configuration loaded: {self.config}")
        self.products = self.config.get("products", [])
        self.owner_contact = self.config.get("owner_contact", {})

        # Initialize business data
        self.selected_product = None
        self.credit_escrow = 0.0
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"  # Default payment method
        logger.debug(f"Initial business data: selected_product={self.selected_product}, credit_escrow={self.credit_escrow:.2f}")

        # Callbacks for UI updates
        self.update_callback = None  # Expected signature: (state, selected_product, credit_escrow)
        self.message_callback = None  # Expected signature: (message)

        # Setup FSM transitions using transitions library, initial state set using VMC.states[0]
        self.machine = Machine(model=self, states=VMC.states, initial=VMC.states[0])
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
        logger.debug("FSM transitions set up successfully.")

        # Remove old PaymentService dependency and initialize PaymentGatewayManager for virtual payments
        self.payment_gateway_manager = PaymentGatewayManager(config=self.config.get("virtual_payment_config", {}))
        self.virtual_payment_index = 0  # Track which virtual payment option to present next

        # Initialize hardware and services
        self.coin_handler = CoinHandler()  # Placeholder for future expansion
        self.mdb_interface = MDBInterface()
        logger.debug("Hardware and services initialized.")

    # --- Condition Methods ---
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        logger.debug(f"Checking credit: {self.credit_escrow:.2f}")
        return self.credit_escrow > 0

    # --- Callback Setters ---
    def set_update_callback(self, callback):
        self.update_callback = callback
        logger.debug("Update callback set.")

    def set_message_callback(self, callback):
        self.message_callback = callback
        logger.debug("Message callback set.")

    # --- Unified Message Routine ---
    def send_customer_message(self, message, tk_root=None, duration=5000):
        """
        Send a message to the customer via the UI.
        All messages go through this method to allow centralized control of timing.
        If tk_root is provided, the message will be cleared after 'duration' milliseconds.
        """
        logger.debug(f"Sending customer message: '{message}'")
        self._display_message(message)
        if tk_root is not None:
            tk_root.after(duration, lambda: self._display_message(""))

    # --- UI Helper Methods ---
    def _refresh_ui(self):
        if self.update_callback:
            logger.debug(f"Refreshing UI with state={self.state}, selected_product={self.selected_product}, credit_escrow={self.credit_escrow:.2f}")
            self.update_callback(self.state, self.selected_product, self.credit_escrow)

    def _display_message(self, message):
        if self.message_callback:
            logger.debug(f"Displaying message: '{message}'")
            self.message_callback(message)

    # --- FSM Callback Methods with Enhanced Logging and Messaging ---
    def on_start_interaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from idle to interacting_with_user for product: {self.selected_product}")
        self._refresh_ui()
        self.send_customer_message("Interaction started. Please insert funds or select a product.")

    def on_dispense_product(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning from interacting_with_user to dispensing for product: {self.selected_product}")
        self._refresh_ui()
        self.send_customer_message("Processing your payment and dispensing your product...")

    def on_complete_transaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Completing transaction. Remaining escrow: ${self.credit_escrow:.2f}")
        self._refresh_ui()
        if self.credit_escrow > 0:
            self.send_customer_message("Transaction complete. You have remaining credit. Please select another product if desired.")
        else:
            self.send_customer_message("Transaction complete. Thank you for your purchase!")

    def on_reset(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Resetting to idle state. Clearing selected product. Previous selection: {self.selected_product}")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._refresh_ui()
        # Message clearing is handled by send_customer_message timing

    def on_error(self):
        logger.error(f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state.")
        self._refresh_ui()
        self.send_customer_message("An error has occurred. Please contact support.")

    # --- Business Logic Methods ---
    def deposit_funds(self, amount, payment_method="Simulated Payment"):
        logger.debug(f"Depositing funds: amount={amount:.2f}, method={payment_method}")
        self.credit_escrow += amount
        self.last_payment_method = payment_method
        logger.info(f"Deposited ${amount:.2f} via {payment_method}. New escrow: ${self.credit_escrow:.2f}")
        self._refresh_ui()
        self.send_customer_message(f"${amount:.2f} deposited. Current balance: ${self.credit_escrow:.2f}.")

    def request_refund(self, tk_root=None):
        logger.debug(f"Requesting refund with current credit: {self.credit_escrow:.2f}")
        if self.credit_escrow > 0:
            refund_amount = self.credit_escrow
            self.credit_escrow = 0.0
            logger.info(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.")
            self.send_customer_message(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.", tk_root)
            self._refresh_ui()
        else:
            self.send_customer_message("No funds to refund.", tk_root)

    def initiate_virtual_payment(self, amount, tk_root):
        """
        Initiates a virtual payment by generating a payment URL and corresponding QR code (stub)
        for the current virtual payment option. Cycles to the next option for future requests.
        """
        gateways = list(self.payment_gateway_manager.gateways.keys())
        if not gateways:
            logger.error("No virtual payment gateways configured.")
            self.send_customer_message("Virtual payment is currently unavailable.", tk_root)
            return

        current_gateway = gateways[self.virtual_payment_index]
        logger.info(f"Initiating virtual payment via {current_gateway} for amount ${amount:.2f}")
        payment_url = self.payment_gateway_manager.gateways[current_gateway].generate_payment_url(amount)
        logger.debug(f"Generated payment URL: {payment_url}")
        # For now, we log and send a customer message; later, the QR code image can be displayed in the UI.
        self.send_customer_message(f"Virtual Payment Option ({current_gateway}): Scan the QR code at {payment_url}", tk_root)
        self.virtual_payment_index = (self.virtual_payment_index + 1) % len(gateways)
        logger.debug(f"Cycled to virtual payment index: {self.virtual_payment_index}")

    def select_product(self, product_index, tk_root):
        logger.debug(f"Selecting product with index: {product_index}")
        if self.state not in ["idle", "interacting_with_user"]:
            logger.warning("Cannot change selection; machine not ready.")
            return
        if product_index >= len(self.products):
            logger.error("Invalid product index selected.")
            return

        self.selected_product = self.products[product_index]
        logger.info(f"Selected product: {self.selected_product.get('name')} at ${self.selected_product.get('price'):.2f}")

        if self.selected_product.get("track_inventory", False):
            if self.selected_product.get("inventory_count", 0) <= 0:
                logger.error(f"{self.selected_product.get('name')} is sold out.")
                self.send_customer_message(f"{self.selected_product.get('name')} is sold out. Please select another product.", tk_root)
                return

        if self.state == "idle":
            self.start_interaction()
            tk_root.after(1000, lambda: self._process_payment(tk_root))
        elif self.state == "interacting_with_user":
            # Initiate virtual payment cycling when the product is re-selected
            self.initiate_virtual_payment(self.selected_product.get("price", 0), tk_root)
            tk_root.after(1000, lambda: self._process_payment(tk_root))
        self._refresh_ui()

    def _update_selection_message(self, tk_root):
        price = self.selected_product.get("price", 0)
        if self.credit_escrow < price:
            required = price - self.credit_escrow
            message = f"Changed selection to {self.selected_product.get('name')}. Insert additional ${required:.2f}."
        else:
            message = f"Changed selection to {self.selected_product.get('name')}. Sufficient funds available."
        logger.debug(f"Updated selection message: {message}")
        self.send_customer_message(message, tk_root)
        self.last_insufficient_message = message

    def _process_payment(self, tk_root):
        logger.debug(f"Processing payment for product: {self.selected_product}")
        if self.state != "interacting_with_user":
            logger.debug("State is not interacting_with_user; aborting payment process.")
            return

        price = self.selected_product.get("price", 0)
        if self.credit_escrow >= price:
            logger.info(f"{STATE_CHANGE_PREFIX} Escrow sufficient ({self.credit_escrow:.2f} >= {price:.2f}). Processing payment.")
            self.send_customer_message("Sufficient funds received. Processing your payment...", tk_root)
            self.credit_escrow -= price
            logger.debug(f"Deducted price from escrow. New escrow: {self.credit_escrow:.2f}")
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
        logger.debug(f"Finishing dispensing process for product: {self.selected_product}")
        if self.state != "dispensing":
            logger.debug("State is not dispensing; cannot finish dispensing.")
            return
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {self.selected_product.get('name')}")
        self.send_customer_message("Product dispensed. Enjoy your purchase!", tk_root)
        if self.selected_product.get("track_inventory", False):
            self.selected_product["inventory_count"] -= 1
            logger.info(f"Inventory for {self.selected_product.get('name')} updated: {self.selected_product['inventory_count']} remaining.")
        self.complete_transaction()
        self._refresh_ui()

    async def start_mdb_monitoring(self):
        logger.debug("Starting MDB monitoring.")
        await self.mdb_interface.read_messages(self.handle_mdb_message)

    def handle_mdb_message(self, message):
        logger.info(f"Received MDB message: {message}")
