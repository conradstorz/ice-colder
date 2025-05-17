# controller/vmc.py
from transitions import Machine

# Global variable for state change log prefix
STATE_CHANGE_PREFIX = "***### STATE CHANGE ###***"

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

class VMCCore:
    # Define FSM states: idle, interacting_with_user, dispensing, error
    states = ["idle", "interacting_with_user", "dispensing", "error"]

    def __init__(self):
        # Data-driven FSM setup: using TRANSITIONS list.
        # Note: ordering matters when multiple transitions share the same trigger name.
        self.machine = Machine(model=self, states=VMCCore.states, initial=VMCCore.states[0])
        # Add all transitions from the TRANSITIONS table.
        for t in TRANSITIONS:
            self.machine.add_transition(**t)

    # --- Condition Methods ---
    def has_credit(self):
        """Return True if there is remaining credit in the escrow."""
        return self.credit_escrow > 0

    # --- FSM Callback Stubs (pure state updates only) ---
    def on_start_interaction(self):
        """Before transitioning to interacting_with_user (no-op)."""
        pass

    def on_dispense_product(self):
        """Before transitioning to dispensing (no-op)."""
        pass

    def on_complete_transaction(self):
        """Before completing a transaction (no-op)."""
        pass

    def on_reset(self):
        """Reset business state after an error."""
        # Reset selected product and last insufficient message
        self.selected_product = None
        self.last_insufficient_message = ""

    def on_error(self):
        """When an error occurs (no-op)."""
        pass
