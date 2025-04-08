# virtual_payment_fsm.py
import asyncio
import random
from loguru import logger

class VirtualPaymentFSM:
    """
    VirtualPaymentFSM is a nested FSM for handling virtual payment interactions.
    It concurrently reaches out to all virtual payment providers (via PaymentGatewayManager),
    gathers the payment URLs (and QR codes), and displays them to the customer.
    The first provider to acknowledge a successful transaction terminates the remaining sessions.
    """

    def __init__(self, payment_manager, amount):
        """
        Initialize the VirtualPaymentFSM.
        
        :param payment_manager: An instance of PaymentGatewayManager containing the available providers.
        :param amount: The amount to be charged.
        """
        self.payment_manager = payment_manager
        self.amount = amount
        self.providers = list(payment_manager.gateways.keys())
        self.selected_provider = None

    async def process_provider(self, provider):
        """
        Simulate processing a payment request with a specific provider.
        
        In a real-world scenario, this method would contact the provider's API,
        generate a URL, display a QR code, and await confirmation of payment.
        For this stub, we simulate a random delay and random success/failure.
        
        :param provider: The provider key (e.g., "stripe", "paypal", etc.).
        :return: The provider if the payment is confirmed.
        :raises Exception: If the payment confirmation fails.
        """
        logger.info(f"VirtualPaymentFSM: Initiating payment request with {provider} for ${self.amount:.2f}")
        # Simulate network/API delay (e.g., between 2 and 10 seconds)
        delay = random.uniform(2, 10)
        await asyncio.sleep(delay)
        # Simulate success/failure (for example purposes, 50% chance)
        success = random.choice([True, False])
        if success:
            logger.info(f"VirtualPaymentFSM: Payment confirmed by {provider} after {delay:.2f} seconds")
            return provider
        else:
            logger.info(f"VirtualPaymentFSM: Payment NOT confirmed by {provider} after {delay:.2f} seconds")
            raise Exception(f"{provider} did not confirm payment")

    async def run(self):
        """
        Run the virtual payment process concurrently for all providers.
        
        This method spawns tasks for each provider and waits until the first one completes successfully.
        It then cancels all pending tasks.
        
        :return: A tuple (status, selected_provider) where status is "completed" or "error"
        """
        if not self.providers:
            logger.error("VirtualPaymentFSM: No virtual payment providers configured.")
            return "error", None

        logger.debug(f"VirtualPaymentFSM: Available virtual payment gateways: {self.providers}")

        # Create tasks for each provider
        tasks = {asyncio.create_task(self.process_provider(provider)): provider for provider in self.providers}
        done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            try:
                self.selected_provider = task.result()
                logger.info(f"VirtualPaymentFSM: Selected provider {self.selected_provider} confirmed payment.")
                break  # We have our successful provider
            except Exception as e:
                logger.error(f"VirtualPaymentFSM: Provider {tasks[task]} error: {e}")

        # Cancel any pending tasks
        for task in pending:
            provider = tasks[task]
            task.cancel()
            logger.debug(f"VirtualPaymentFSM: Cancelled pending payment request for {provider}")

        if self.selected_provider:
            return "completed", self.selected_provider
        else:
            return "error", None

# Example usage:
# In your VMC, you might call:
#
# async def run_virtual_payment_process(self, tk_root):
#     logger.debug("Main FSM: Starting virtual payment process.")
#     payment_fsm = VirtualPaymentFSM(self.payment_gateway_manager, self.selected_product.get("price", 0))
#     status, provider = await payment_fsm.run()
#     if status == "completed":
#         logger.info(f"Main FSM: Virtual payment successful via {provider}.")
#         self.send_customer_message(f"Payment completed via {provider}.", tk_root)
#         # Here you would load the payment amount into the machine escrow, etc.
#     else:
#         logger.error("Main FSM: Virtual payment failed.")
#         self.send_customer_message("Virtual payment failed. Please try again or use cash.", tk_root)
#
# And then in your VMC._process_payment(), you would call:
#
#     asyncio.create_task(self.run_virtual_payment_process(tk_root))
#
# This design separates the virtual payment logic from the main FSM while allowing asynchronous handling.
