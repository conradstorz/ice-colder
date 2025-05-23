# virtual_payment_fsm.py
import asyncio
from loguru import logger
from async_payment_fsm import AsyncPaymentFSM

class VirtualPaymentFSM(AsyncPaymentFSM):
    """
    Asynchronous FSM for managing virtual payment options.
    Polls virtual payment providers asynchronously and reports events (including refunds) via the callback.
    Each provider in payment_gateways must implement:
      - generate_payment_url(amount)
      - check_payment_status() returning "success", "pending", or "timeout"
      - process_refund(amount) for processing refunds (this could be simulated)
    """
    def __init__(self, payment_gateways, callback=None, poll_interval=1.0):
        super().__init__("VirtualPaymentFSM", callback=callback)
        self.payment_gateways = payment_gateways
        self.poll_interval = poll_interval
        self.virtual_payment_tasks = []
        self.active = False
        self.status = {"state": "idle"}
        # Store the successful gateway once a payment is completed
        self.successful_gateway = None

    async def start_transaction(self, amount: float):
        """
        Initiates asynchronous virtual payment transactions across all providers.
        Returns the name of the successful provider, or None if failure.
        """
        self.active = True
        self.status["state"] = "processing"
        logger.info(f"VirtualPaymentFSM: Starting virtual payment for amount: ${amount:.2f}")
        tasks = []
        for gateway in self.payment_gateways:
            task = asyncio.create_task(self._poll_gateway(gateway, amount))
            tasks.append(task)
            self.virtual_payment_tasks.append(task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        self.successful_gateway = None
        for task in done:
            result = task.result()
            if result is not None:
                self.successful_gateway = result
                break

        # Cancel any pending tasks
        for task in pending:
            task.cancel()
        self.active = False
        if self.successful_gateway:
            self.status["state"] = "success"
            self.notify("payment_success", {"gateway": self.successful_gateway})
        else:
            self.status["state"] = "failure"
            self.notify("payment_failure", {})
        self.virtual_payment_tasks = []
        return self.successful_gateway

    async def cancel_transaction(self):
        """
        Cancels any ongoing virtual payment transactions.
        """
        if self.virtual_payment_tasks:
            logger.info("VirtualPaymentFSM: Cancelling virtual payment tasks.")
            for task in self.virtual_payment_tasks:
                task.cancel()
            self.virtual_payment_tasks = []
            self.status["state"] = "cancelled"
            self.notify("payment_cancelled", {})
            await asyncio.sleep(0)
        else:
            logger.debug("VirtualPaymentFSM: No active virtual payment tasks to cancel.")

    async def get_status(self) -> dict:
        logger.debug(f"VirtualPaymentFSM: Current status: {self.status}")
        return self.status

    async def dispense_change(self):
        # For virtual payments, dispensing change is not applicable.
        logger.debug("VirtualPaymentFSM: No change dispensing required for virtual payments.")
        await asyncio.sleep(0)

    async def refund(self, amount: float):
        """
        Process a refund via the virtual payment system.
        In a real implementation, this would call the provider's refund API.
        Here we simulate a refund if a successful gateway has been used.
        """
        if self.successful_gateway is None:
            logger.error("VirtualPaymentFSM: Cannot process refund because no payment was completed.")
            self.notify("refund_failed", {"reason": "No completed payment"})
            return None
        # Simulate asynchronous refund processing.
        await asyncio.sleep(self.poll_interval)
        logger.info(f"VirtualPaymentFSM: Refunding ${amount:.2f} via {self.successful_gateway}.")
        self.notify("refund_processed", {"gateway": self.successful_gateway, "refund_amount": amount})
        return amount

    async def _poll_gateway(self, gateway_name, amount):
        provider = self.payment_gateways[gateway_name]
        self.notify("payment_request", {"gateway": gateway_name, "status": "requested"})
        payment_url = provider.generate_payment_url(amount)
        self.notify("payment_url", {"gateway": gateway_name, "url": payment_url})
        try:
            for i in range(10):
                await asyncio.sleep(self.poll_interval)
                status = provider.check_payment_status()  # returns "success", "pending", or "timeout"
                if status == "success":
                    self.notify("payment_success", {"gateway": gateway_name, "url": payment_url})
                    return gateway_name
                elif status == "timeout":
                    self.notify("payment_timeout", {"gateway": gateway_name})
                    return None
                else:
                    self.notify("payment_pending", {"gateway": gateway_name})
            self.notify("payment_timeout", {"gateway": gateway_name})
            return None
        except asyncio.CancelledError:
            logger.info(f"VirtualPaymentFSM: Polling cancelled for gateway: {gateway_name}")
            self.notify("payment_cancelled", {"gateway": gateway_name})
            raise
