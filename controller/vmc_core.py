# ========== vmc_core.py ==========
"""
vmc_core.py

Pure finite state machine (FSM) definition for the Vending Machine Controller (VMC).
This module contains only the state definitions, transition table, and simple callbacks
that update internal state without any direct hardware or UI interaction.
"""
from ChatGPT_03mini_fsm_vmc import event_store
from ChatGPT_03mini_fsm_vmc.event_store import TransactionEvent
from ChatGPT_03mini_fsm_vmc.state_model import MachineState
from loguru import logger
from transitions import Machine

# Prefix for any logged state changes, to make them easily searchable in logs
STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

# Reference to the event store for snapshot and event operations
store = event_store

#: FSM transition table.
#: **Note**: ordering matters when multiple transitions share the same trigger name.
#: For example, when you call `complete_transaction` from the "dispensing" state:
#: 1. The first dict (with `conditions="has_credit"`) is evaluated—if `has_credit()` is True,
#:    you go to `"interacting_with_user"`.
#: 2. Otherwise, the second dict (with `unless="has_credit"`) fires, taking you back to `"idle"`.
TRANSITIONS = [
    # User starts interacting → go from idle to interacting_with_user
    {
        "trigger": "start_interaction",
        "source": "idle",
        "dest": "interacting_with_user",
        "before": "on_start_interaction",
    },
    # Dispense button pressed → enter dispensing state
    {
        "trigger": "dispense_product",
        "source": "interacting_with_user",
        "dest": "dispensing",
        "before": "on_dispense_product",
    },
    # Complete transaction branch #1: if there's still credit, return to interacting_with_user
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "interacting_with_user",
        "conditions": "has_credit",
        "before": "on_complete_transaction",
    },
    # Complete transaction branch #2: if no credit remains, go back to idle
    {
        "trigger": "complete_transaction",
        "source": "dispensing",
        "dest": "idle",
        "unless": "has_credit",
        "before": "on_complete_transaction",
    },
    # Any error moves state machine to error
    {
        "trigger": "error_occurred",
        "source": "*",
        "dest": "error",
        "before": "on_error",
    },
    # After handling error, reset back to idle
    {
        "trigger": "reset_state",
        "source": ["error"],
        "dest": "idle",
        "before": "on_reset",
    },
]

class VMC:
    """
    Vending Machine Controller core class.
    Maintains only the business logic state and transitions.
    Does NOT perform any hardware access or user interface updates.
    """
    # Define the four possible states of the FSM.
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    def __init__(self, products, owner_contact):
        """
        Initialize the pure FSM with given products list and owner contact info.

        :param products: list of dicts, each describing a product (name, price, inventory, etc.)
        :param owner_contact: contact information for machine owner (for maintenance alerts)
        """
        logger.debug(f"{STATE_CHANGE_PREFIX} Initializing core FSM")
        # Save business data locally
        self.products = products
        self.owner_contact = owner_contact

        # Initialize runtime state variables
        self.selected_product = None  # Currently chosen product dict
        self.credit_escrow = 0.0     # Funds inserted but not yet spent
        self.last_payment_method = None  # Last method used for deposit or refund

        # recover previous state from event store  TODO: make this code work
        snapshot = store.load_latest_snapshot()
        state = MachineState(**snapshot.state) if snapshot else MachineState(fsm_state="startup")
        # then replay:
        tx_events = store.replay_events(TransactionEvent)
        for tx in tx_events:
            state.record_transaction(tx.channel, tx.sku, tx.amount, tx.timestamp)

        # Create the state machine and register transitions
        self.machine = Machine(
            model=self,
            states=VMC.states,
            initial="idle",
            auto_transitions=False,
            ignore_invalid_triggers=True,
        )
        for t in TRANSITIONS:
            # Add each transition entry to the state machine
            self.machine.add_transition(**t)
        logger.debug(f"{STATE_CHANGE_PREFIX} FSM transitions configured: {TRANSITIONS}")

    def has_credit(self):
        """
        Condition method: returns True if there is remaining credit in the escrow.
        """
        return self.credit_escrow > 0

    # --- FSM callback methods: these only update internal data or log actions. ---
    def on_start_interaction(self):
        """Callback before moving from 'idle' to 'interacting_with_user'."""
        logger.info(f"{STATE_CHANGE_PREFIX} Started interaction; selected_product={self.selected_product}")

    def on_dispense_product(self):
        """Callback before moving from 'interacting_with_user' to 'dispensing'."""
        logger.info(f"{STATE_CHANGE_PREFIX} Dispensing product; selected_product={self.selected_product}")

    def on_complete_transaction(self):
        """Callback before returning to 'interacting_with_user' or 'idle'."""
        evt = TransactionEvent(
            channel=self.channel.value,
            sku=self.sku,
            amount=self.amount,
            fsm_state_before=self.old_state,
            fsm_state_after=self.new_state
        )
        store.append_event(evt)
        store.checkpoint(self.__dict__)
        logger.info(f"{STATE_CHANGE_PREFIX} Completing transaction; remaining credit={self.credit_escrow:.2f}")

    def on_error(self):
        """Callback when an error occurs; transitions to 'error' state."""
        logger.error(f"{STATE_CHANGE_PREFIX} Entered error state; selected_product={self.selected_product}")

    def on_reset(self):
        """Callback before resetting from 'error' back to 'idle'."""
        logger.info(f"{STATE_CHANGE_PREFIX} Resetting FSM; clearing selected_product and escrow.")
        self.selected_product = None
        self.credit_escrow = 0.0

    # --- Pure business logic methods: deposit, refund, select, and process payment. ---
    def deposit_funds(self, amount, method="Simulated Payment"):
        """
        Add funds to escrow.

        :param amount: float, amount to deposit
        :param method: payment method identifier
        """
        logger.debug(f"Depositing funds: amount={amount:.2f}, method={method}")
        self.credit_escrow += amount
        self.last_payment_method = method

    def request_refund(self):
        """
        Refund all credit in escrow.

        :return: float, the refunded amount
        """
        if self.credit_escrow <= 0:
            logger.debug("No funds to refund.")
            return 0.0
        refund_amount = self.credit_escrow
        self.credit_escrow = 0.0
        logger.info(f"Refund issued: amount={refund_amount:.2f}")
        return refund_amount

    def select_product(self, product_index):
        """
        Choose a product by index and transition FSM accordingly.

        :param product_index: int, index into self.products list
        """
        logger.debug(f"Selecting product at index={product_index}")
        if product_index < 0 or product_index >= len(self.products):
            logger.error("Invalid selection index.")
            return
        self.selected_product = self.products[product_index]
        # Trigger interaction start if necessary
        self.start_interaction()

    def process_payment(self):
        """
        Check escrow against selected product price, dispense if sufficient,
        or signal need for more funds.

        :return: tuple(bool dispensed, float needed_amount)
        """
        price = self.selected_product.get("price", 0)
        if self.credit_escrow >= price:
            self.credit_escrow -= price
            self.dispense_product()
            return True, 0.0
        needed = price - self.credit_escrow
        logger.warning(f"Insufficient funds; need additional {needed:.2f}")
        return False, needed

