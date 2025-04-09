# virtual_payment_fsm.py
import asyncio
from loguru import logger

class VirtualPaymentFSM:
    """
    FSM for managing virtual payment options asynchronously.
    This FSM continuously polls virtual payment providers and communicates with the primary VMC.
    """
    def __init__(self, payment_gateways, callback=None, poll_interval=1.0):
        """
        :param payment_gateways: A dict of virtual payment gateway instances.
                                 Each provider must implement:
                                     - generate_payment_url(amount)
                                     - check_payment_status() which returns "success", "pending", or "timeout"
        :param callback: Callback function for communicating events to the primary VMC.
                         Expected signature: callback(event_type: str, data: dict)
        :param poll_interval: Interval in seconds to poll for updates.
        """
        self.payment_gateways = payment_gateways
        self.callback = callback
        self.poll_interval = poll_interval
        self.virtual_payment_tasks = []
        self.active = False

    def register_callback(self, callback):
        self.callback = callback
        logger.debug("VirtualPaymentFSM: Callback registered.")

    def notify(self, event_type, data):
        logger.info(f"VirtualPaymentFSM: Notifying event '{event_type}' with data: {data}")
        if self.callback:
            self.callback(event_type, data)

    async def _poll_gateway(self, gateway_name, amount):
        """
        Poll a single virtual payment gateway for a payment status update.
        This simulates asynchronous checking; in a real scenario, you might use an async HTTP client.
        """
        provider = self.payment_gateways[gateway_name]
        # Request the payment URL
        self.notify("payment_request", {"gateway": gateway_name, "status": "requested"})
        payment_url = provider.generate_payment_url(amount)
        self.notify("payment_url", {"gateway": gateway_name, "url": payment_url})

        try:
            # Poll the provider for up to 10 iterations
            for i in range(10):
                await asyncio.sleep(self.poll_interval)
                status = provider.check_payment_status()  # Expected to return "success", "pending", or "timeout"
                if status == "success":
                    self.notify("payment_success", {"gateway": gateway_name, "url": payment_url})
                    return gateway_name
                elif status == "timeout":
                    self.notify("payment_timeout", {"gateway": gateway_name})
                    return None
                else:
                    self.notify("payment_pending", {"gateway": gateway_name})
            # If no success after polling iterations, treat as a timeout
            self.notify("payment_timeout", {"gateway": gateway_name})
            return None
        except asyncio.CancelledError:
            logger.info(f"VirtualPaymentFSM: Polling cancelled for gateway: {gateway_name}")
            self.notify("payment_cancelled", {"gateway": gateway_name})
            raise

    async def start_virtual_payment(self, amount):
        """
        Initiates asynchronous virtual payment requests for all providers.
        As soon as one provider reports success, all pending tasks are cancelled.
        :param amount: The payment amount.
        :return: The name of the successful provider (if any) or None.
        """
        self.active = True
        logger.info("VirtualPaymentFSM: Starting virtual payment process.")
        tasks = []
        for gateway in self.payment_gateways:
            task = asyncio.create_task(self._poll_gateway(gateway, amount))
            tasks.append(task)
            self.virtual_payment_tasks.append(task)

        # Wait until one task completes successfully or all tasks complete.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        successful_gateway = None
        for task in done:
            result = task.result()
            if result is not None:
                successful_gateway = result
                break

        # Cancel any remaining tasks.
        for task in pending:
            task.cancel()
        self.active = False
        if successful_gateway:
            self.notify("virtual_payment_complete", {"successful_gateway": successful_gateway})
        else:
            self.notify("virtual_payment_failure", {})
        # Clear the task list
        self.virtual_payment_tasks = []
        return successful_gateway

    def cancel_virtual_payment(self):
        """
        Cancels any ongoing virtual payment tasks.
        """
        logger.info("VirtualPaymentFSM: Cancelling virtual payment tasks.")
        for task in self.virtual_payment_tasks:
            task.cancel()
        self.virtual_payment_tasks = []
        self.notify("virtual_payment_cancelled", {})
