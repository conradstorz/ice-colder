# dispensing_fsm.py
import asyncio
from transitions import Machine
from loguru import logger

class DispenseFSM:
    # Define states for the dispensing process
    states = ["init", "activating", "verifying", "completed", "error"]

    def __init__(self):
        # Initialize the state machine with the first state: "init"
        self.machine = Machine(model=self, states=DispenseFSM.states, initial="init")
        # Define transitions for the dispensing process
        self.machine.add_transition(trigger="start_activation", source="init", dest="activating")
        self.machine.add_transition(trigger="verify_dispense", source="activating", dest="verifying")
        self.machine.add_transition(trigger="complete_dispense", source="verifying", dest="completed")
        # Any failure at any state will lead to "error"
        self.machine.add_transition(trigger="fail", source="*", dest="error")
    
    async def run(self):
        try:
            logger.info("DispenseFSM: Starting dispensing process.")
            # Transition to activating state
            self.start_activation()
            logger.info("DispenseFSM: Activating motor for product dispense.")
            # Simulate motor activation time (e.g., 10 seconds, could be up to 90 seconds in real-world)
            await asyncio.sleep(10)

            # Transition to verifying state
            self.verify_dispense()
            logger.info("DispenseFSM: Verifying product dispense.")
            # Simulate verification process (for example, sensor check)
            await asyncio.sleep(5)

            # Simulate condition: you might insert logic here to detect errors
            # For now, assume successful dispensing:
            self.complete_dispense()
            logger.info("DispenseFSM: Dispensing process completed successfully.")
            return "completed"
        except Exception as e:
            self.fail()
            logger.error(f"DispenseFSM: Error during dispensing process: {e}")
            return "error"

# Example usage within VMC (main controller) in vmc.py:
#
# async def run_dispense_process(self, tk_root):
#     logger.debug("Starting nested dispensing FSM process.")
#     dispense_fsm = DispenseFSM()
#     result = await dispense_fsm.run()
#     if result == "completed":
#         # Trigger main FSM transition for successful dispense completion
#         self.complete_transaction()
#         logger.info("Main FSM: Dispense process completed; transaction completed.")
#         self.send_customer_message("Product dispensed successfully.", tk_root)
#     else:
#         # Handle error during dispensing
#         self.error_occurred()
#         logger.error("Main FSM: Dispense process failed; transitioning to error state.")
#         self.send_customer_message("Error during product dispensing. Please contact support.", tk_root)
#
# In your main VMC.select_product() or _process_payment() method, you would call:
#
#     asyncio.create_task(self.run_dispense_process(tk_root))
#
# This way, the dispensing process runs asynchronously as its own FSM, and the main FSM remains responsive.
