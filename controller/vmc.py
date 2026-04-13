# controller/vmc.py
import asyncio
import time
from transitions import Machine
from loguru import logger
from services.payment_gateway_manager import PaymentGatewayManager
from services.mqtt_messages import VMCStatus, PaymentEvent, ButtonPress
from config.config_model import ConfigModel
from services.health_monitor import HealthMonitor

STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

#: FSM transition table.
#: Ordering matters when multiple transitions share the same trigger name.
TRANSITIONS = [
    {
        "trigger": "start_interaction",
        "source": "idle",
        "dest": "interacting_with_user",
        "before": "on_start_interaction",
    },
    {
        "trigger": "dispense_product",
        "source": "interacting_with_user",
        "dest": "dispensing",
        "before": "on_dispense_product",
    },
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "interacting_with_user",
        "conditions": "has_credit",
        "before": "on_complete_transaction",
    },
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "idle",
        "unless": "has_credit",
        "before": "on_complete_transaction",
    },
    {
        "trigger": "error_occurred",
        "source": "*",
        "dest": "error",
        "before": "on_error",
    },
    {
        "trigger": "reset_state",
        "source": ["error"],
        "dest": "idle",
        "before": "on_reset",
    },
]

class VMC:
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    @logger.catch()
    def __init__(self, config: ConfigModel):
        logger.debug("Initializing VMC with pre-loaded ConfigModel")

        self.config_model = config
        logger.debug(self.config_model.model_dump_json(exclude_none=True, indent=2))

        self.products = self.config_model.products
        self.owner_contact = self.config_model.machine_owner

        self.selected_product = None
        self.credit_escrow = 0.0
        self.last_insufficient_message = ""
        self.last_payment_method = "Simulated Payment"

        self.update_callback = None
        self.message_callback = None
        self.qrcode_callback = None

        self._pending_tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_client = None  # Set via set_mqtt_client()
        self._health_monitor: HealthMonitor | None = None  # Set via set_health_monitor()
        self._start_time = time.monotonic()

        self.machine = Machine(model=self, states=VMC.states, initial=VMC.states[0], auto_transitions=False)

        for t in TRANSITIONS:
            self.machine.add_transition(**t)
        logger.debug("FSM transitions set up successfully.")

        self.payment_gateway_manager = PaymentGatewayManager(config=self.config_model.payment.model_dump())
        self.virtual_payment_index = 0

        logger.debug("VMC initialization complete.")

    def attach_to_loop(self, loop: asyncio.AbstractEventLoop):
        """Attach VMC to the running asyncio event loop. Must be called before scheduling."""
        self._loop = loop
        logger.debug("VMC attached to asyncio event loop.")

    def set_mqtt_client(self, client):
        """Attach an MQTTClient instance for publishing status and receiving events."""
        self._mqtt_client = client
        # Register handlers for inbound ESP32 messages
        client.register("payment/credit", self._handle_mqtt_payment)
        client.register("hardware/buttons", self._handle_mqtt_button)
        client.register("hardware/dispenser", self._handle_mqtt_dispenser)
        client.register("sensors/temp/+", self._handle_mqtt_sensor)
        client.register("heartbeat/+", self._handle_mqtt_heartbeat)
        logger.debug("VMC registered MQTT handlers.")

    def set_health_monitor(self, monitor: HealthMonitor):
        """Attach a HealthMonitor so MQTT events feed into health tracking."""
        self._health_monitor = monitor
        logger.debug("VMC attached health monitor.")

    def _publish_status(self):
        """Publish current VMC status to MQTT (fire-and-forget)."""
        if self._mqtt_client is None or self._loop is None:
            return
        status = VMCStatus(
            state=self.state,
            credit_escrow=self.credit_escrow,
            selected_product=self.selected_product.name if self.selected_product else None,
            uptime_seconds=int(time.monotonic() - self._start_time),
        )
        self._loop.create_task(self._mqtt_client.publish("status", status))
        if self._health_monitor:
            self._health_monitor.update_vmc_state(self.state)

    # --- MQTT inbound handlers ---

    async def _handle_mqtt_payment(self, topic: str, data: dict):
        """Handle payment credit from MDB ESP32."""
        event = PaymentEvent.model_validate(data)
        logger.info(f"MQTT payment received: ${event.amount:.2f} via {event.method}")
        self.deposit_funds(event.amount, payment_method=event.method)

    async def _handle_mqtt_button(self, topic: str, data: dict):
        """Handle button press from ESP32."""
        press = ButtonPress.model_validate(data)
        logger.info(f"MQTT button press: button {press.button}")
        self.select_product(press.button)

    async def _handle_mqtt_dispenser(self, topic: str, data: dict):
        """Handle dispenser status from ESP32."""
        logger.info(f"MQTT dispenser event: {data}")
        state = data.get("state", "")
        if state == "complete" and self.state == "dispensing":
            self._finish_dispensing()
        elif state in ("jammed", "error"):
            logger.error(f"Dispenser error: {state}")
            self.error_occurred()

    async def _handle_mqtt_sensor(self, topic: str, data: dict):
        """Handle temperature/sensor reading from ESP32."""
        logger.debug(f"MQTT sensor [{topic}]: {data}")
        if self._health_monitor:
            location = data.get("location", topic.split("/")[-1] if "/" in topic else topic)
            value = data.get("value")
            if value is not None:
                self._health_monitor.record_temperature(location, float(value))

    async def _handle_mqtt_heartbeat(self, topic: str, data: dict):
        """Handle heartbeat from ESP32 subsystem."""
        logger.debug(f"MQTT heartbeat [{topic}]: {data}")
        if self._health_monitor:
            subsystem = data.get("subsystem", topic.split("/")[-1] if "/" in topic else topic)
            self._health_monitor.record_heartbeat(subsystem, data)

    def _schedule(self, delay_seconds, callback):
        """Schedule a synchronous callback to run after delay_seconds on the event loop."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("No event loop attached; cannot schedule callback.")
            return

        async def _delayed():
            await asyncio.sleep(delay_seconds)
            callback()

        task = self._loop.create_task(_delayed())
        self._pending_tasks.append(task)
        # Clean up finished tasks
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "selected_product": self.selected_product.name if self.selected_product else None,
            "credit_escrow": self.credit_escrow,
            "last_payment_method": self.last_payment_method,
        }

    @logger.catch()
    def set_qrcode_callback(self, callback):
        self.qrcode_callback = callback

    @logger.catch()
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        return self.credit_escrow > 0

    @logger.catch()
    def set_update_callback(self, callback):
        self.update_callback = callback

    @logger.catch()
    def set_message_callback(self, callback):
        self.message_callback = callback

    @logger.catch()
    def send_customer_message(self, message):
        """Send a message to the customer via the registered callback."""
        logger.debug(f"Sending customer message: '{message}'")
        self._display_message(message)

    @logger.catch()
    def _refresh_ui(self):
        if self.update_callback:
            self.update_callback(self.state, self.selected_product, self.credit_escrow)

    @logger.catch()
    def _display_message(self, message):
        if self.message_callback:
            self.message_callback(message)

    # --- FSM Callback Methods ---
    @logger.catch()
    def on_start_interaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning to interacting_with_user for product: {self.selected_product}")
        self._publish_status()
        self._refresh_ui()
        self.send_customer_message("Interaction started. Please insert funds or select a product.")

    @logger.catch()
    def on_dispense_product(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Transitioning to dispensing for product: {self.selected_product}")
        self._publish_status()
        self._refresh_ui()
        self.send_customer_message("Processing your payment and dispensing your product...")

    @logger.catch()
    def on_complete_transaction(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Completing transaction. Remaining escrow: ${self.credit_escrow:.2f}")
        self._publish_status()
        self._refresh_ui()
        if self.credit_escrow > 0:
            self.send_customer_message("Transaction complete. You have remaining credit. Please select another product if desired.")
        else:
            self.send_customer_message("Transaction complete. Thank you for your purchase!")

    @logger.catch()
    def on_reset(self):
        logger.info(f"{STATE_CHANGE_PREFIX} Resetting to idle state. Previous selection: {self.selected_product}")
        self.selected_product = None
        self.last_insufficient_message = ""
        self._publish_status()
        self._refresh_ui()

    @logger.catch()
    def on_error(self):
        logger.error(f"{STATE_CHANGE_PREFIX} Error encountered for product: {self.selected_product}. Transitioning to error state.")
        self._publish_status()
        self._refresh_ui()
        self.send_customer_message("An error has occurred. Please contact support.")

    # --- Business Logic Methods ---
    @logger.catch()
    def deposit_funds(self, amount, payment_method="Simulated Payment"):
        logger.debug(f"Depositing funds: amount={amount:.2f}, method={payment_method}")
        self.credit_escrow += amount
        self.last_payment_method = payment_method
        logger.info(f"Deposited ${amount:.2f} via {payment_method}. New escrow: ${self.credit_escrow:.2f}")
        self._publish_status()
        self._refresh_ui()
        self.send_customer_message(f"${amount:.2f} deposited. Current balance: ${self.credit_escrow:.2f}.")

    @logger.catch()
    def request_refund(self):
        logger.debug(f"Requesting refund with current credit: {self.credit_escrow:.2f}")
        if self.credit_escrow > 0:
            refund_amount = self.credit_escrow
            self.credit_escrow = 0.0
            logger.info(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.")
            self.send_customer_message(f"Refund of ${refund_amount:.2f} issued via {self.last_payment_method}.")
            self._refresh_ui()
        else:
            self.send_customer_message("No funds to refund.")

    @logger.catch()
    def initiate_virtual_payment(self, amount):
        """
        Initiates a virtual payment by generating a payment URL and corresponding QR code.
        Cycles through available virtual payment gateways.
        """
        gateways = list(self.payment_gateway_manager.gateways.keys())
        logger.debug(f"Available virtual payment gateways: {gateways}")
        if not gateways:
            logger.error("No virtual payment gateways configured.")
            self.send_customer_message("Virtual payment is currently unavailable.")
            return

        current_gateway = gateways[self.virtual_payment_index]
        logger.info(f"Initiating virtual payment via {current_gateway} for amount ${amount:.2f}")
        payment_url = self.payment_gateway_manager.gateways[current_gateway].generate_payment_url(amount)
        logger.debug(f"Generated payment URL: {payment_url}")

        qr_image = self.payment_gateway_manager.generate_qr_code(current_gateway, amount)
        if self.qrcode_callback:
            self.qrcode_callback(qr_image)
        self.send_customer_message(f"Virtual Payment Option ({current_gateway}): Scan the QR code above.")
        self.virtual_payment_index = (self.virtual_payment_index + 1) % len(gateways)

    @logger.catch()
    def select_product(self, product_index):
        logger.debug(f"Selecting product with index: {product_index}")
        if self.state not in ["idle", "interacting_with_user"]:
            logger.warning("Cannot change selection; machine not ready.")
            return
        if product_index >= len(self.products):
            logger.error("Invalid product index selected.")
            return

        self.selected_product = self.products[product_index]
        logger.info(f"Selected product: {self.selected_product.name} at ${self.selected_product.price:.2f}")

        if self.selected_product.track_inventory:
            if self.selected_product.inventory_count <= 0:
                logger.error(f"{self.selected_product.name} is sold out.")
                self.send_customer_message(f"{self.selected_product.name} is sold out. Please select another product.")
                return

        if self.state == "idle":
            self.start_interaction()
            self._schedule(1.0, self._process_payment)
        elif self.state == "interacting_with_user":
            self.initiate_virtual_payment(self.selected_product.price)
            self._schedule(1.0, self._process_payment)
        self._refresh_ui()

    @logger.catch()
    def _update_selection_message(self):
        price = self.selected_product.price if self.selected_product else 0
        if self.selected_product:
            if self.credit_escrow < price:
                required = price - self.credit_escrow
                message = f"Changed selection to {self.selected_product.name}. Insert additional ${required:.2f}."
            else:
                message = f"Changed selection to {self.selected_product.name}. Sufficient funds available."
        else:
            message = "No product selected."
        logger.debug(f"Updated selection message: {message}")
        self.send_customer_message(message)
        self.last_insufficient_message = message

    @logger.catch()
    def _process_payment(self):
        logger.debug(f"Processing payment for product: {self.selected_product}")
        if self.state != "interacting_with_user":
            logger.debug("State is not interacting_with_user; aborting payment process.")
            return

        price = self.selected_product.price if self.selected_product else 0
        if self.credit_escrow >= price:
            logger.info(f"{STATE_CHANGE_PREFIX} Escrow sufficient ({self.credit_escrow:.2f} >= {price:.2f}). Processing payment.")
            self.send_customer_message("Sufficient funds received. Processing your payment...")
            self.credit_escrow -= price
            logger.debug(f"Deducted price from escrow. New escrow: {self.credit_escrow:.2f}")
            self.dispense_product()
            self._refresh_ui()
            self._schedule(1.0, self._finish_dispensing)
            self.last_insufficient_message = ""
        else:
            required = price - self.credit_escrow
            message = f"Insufficient funds. Please insert an additional ${required:.2f}."
            if message != self.last_insufficient_message:
                logger.error(message)
                self.send_customer_message(message)
                self.last_insufficient_message = message
            self._schedule(5.0, self._process_payment)

    @logger.catch()
    def _finish_dispensing(self):
        logger.debug(f"Finishing dispensing process for product: {self.selected_product}")
        if self.state != "dispensing":
            logger.debug("State is not dispensing; cannot finish dispensing.")
            return
        product_name = self.selected_product.name if self.selected_product else "Unknown"
        logger.info(f"{STATE_CHANGE_PREFIX} Finished dispensing: {product_name}")
        self.send_customer_message("Product dispensed. Enjoy your purchase!")
        if self.selected_product and self.selected_product.track_inventory:
            self.selected_product.inventory_count -= 1
            logger.info(f"Inventory for {self.selected_product.name} updated: {self.selected_product.inventory_count} remaining.")
        self.complete_transaction()
        self._refresh_ui()

