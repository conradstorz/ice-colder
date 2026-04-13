# simulators/mdb_gateway.py
"""
MDB payment gateway simulator.

Simulates MDB bus devices (coin acceptor, bill validator, card reader).
Watches VMC status via MQTT and reactively inserts payments when a
customer interaction is detected.

Run: uv run python -m simulators.mdb_gateway [--broker HOST] [--port PORT] [--machine-id ID]
"""
import asyncio
import json
import random

import aiomqtt
from loguru import logger

from simulators.base import ESP32Simulator
from services.mqtt_messages import PaymentEvent, PaymentStatus


class PaymentStrategy:
    """Encapsulates the randomized payment logic."""

    COIN_DENOMS = [0.25, 0.50, 1.00]
    BILL_DENOMS = [1.00, 5.00, 10.00, 20.00]
    METHODS = ["cash_coin", "cash_bill", "card", "nfc"]

    def pick_method(self) -> str:
        return random.choice(self.METHODS)

    def pick_coin(self) -> float:
        return random.choice(self.COIN_DENOMS)

    def pick_bill(self) -> float:
        return random.choice(self.BILL_DENOMS)

    def card_amount(self, price: float) -> float:
        """Return a card payment amount — sometimes exact, sometimes not."""
        roll = random.random()
        if roll < 0.4:
            # Underpay (partial auth)
            return round(price * random.uniform(0.3, 0.9), 2)
        elif roll < 0.8:
            # Exact or slight overpay
            return round(price * random.uniform(1.0, 1.1), 2)
        else:
            # Significant overpay
            return round(price * random.uniform(1.5, 3.0), 2)


class MDBGatewaySimulator(ESP32Simulator):
    """Simulates MDB payment devices reacting to VMC state."""

    DEVICE_STATUS_INTERVAL = 30.0  # seconds between device status publishes
    MAX_CASH_ATTEMPTS = 3

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="mdb", **kwargs)
        self.strategy = PaymentStrategy()
        self.devices = [
            {"name": "coin_acceptor", "state": "ready"},
            {"name": "bill_validator", "state": "ready"},
            {"name": "card_reader", "state": "ready"},
        ]
        self._vmc_status: asyncio.Queue = asyncio.Queue()

    async def _publish_device_status(self, client: aiomqtt.Client):
        """Periodically publish device readiness status."""
        while True:
            for device in self.devices:
                await self.publish(
                    client, "payment/status",
                    PaymentStatus(device=device["name"], state=device["state"]),
                )
            logger.debug("[mdb] Published device status")
            await asyncio.sleep(self.DEVICE_STATUS_INTERVAL)

    async def _watch_vmc_status(self, client: aiomqtt.Client):
        """Subscribe to VMC status and forward state changes to the payment loop."""
        topic = f"{self.topic_prefix}/status"
        await client.subscribe(topic)
        logger.info(f"[mdb] Subscribed to {topic}")
        async for message in client.messages:
            try:
                data = json.loads(message.payload)
                await self._vmc_status.put(data)
            except (json.JSONDecodeError, TypeError):
                pass

    async def _payment_loop(self, client: aiomqtt.Client):
        """React to VMC state changes by inserting payments."""
        while True:
            # Wait for a status update
            status = await self._vmc_status.get()
            state = status.get("state", "")

            if state != "interacting_with_user":
                continue

            selected = status.get("selected_product")
            if not selected:
                continue

            logger.info(f"[mdb] Customer interaction detected, product: {selected}")

            # Simulate customer reaching for wallet
            await asyncio.sleep(random.uniform(2.0, 5.0))

            method = self.strategy.pick_method()
            logger.info(f"[mdb] Payment method: {method}")

            if method in ("card", "nfc"):
                await self._do_card_payment(client, method)
            else:
                await self._do_cash_payment(client, method)

    async def _do_cash_payment(self, client: aiomqtt.Client, method: str):
        """Insert cash denominations, possibly requiring multiple attempts."""
        for attempt in range(self.MAX_CASH_ATTEMPTS):
            if method == "cash_coin":
                amount = self.strategy.pick_coin()
            else:
                amount = self.strategy.pick_bill()

            await self.publish(
                client, "payment/credit",
                PaymentEvent(amount=amount, method=method),
            )
            logger.info(f"[mdb] Inserted ${amount:.2f} via {method} (attempt {attempt + 1})")

            # Wait and check if VMC moved past interacting state
            await asyncio.sleep(random.uniform(3.0, 8.0))

            # Drain the queue to get latest status
            latest = None
            while not self._vmc_status.empty():
                try:
                    latest = self._vmc_status.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if latest and latest.get("state") != "interacting_with_user":
                logger.info("[mdb] VMC moved on, payment sufficient")
                return

        logger.info("[mdb] Max cash attempts reached")

    async def _do_card_payment(self, client: aiomqtt.Client, method: str):
        """Insert a card/NFC payment — single transaction."""
        # Use a reasonable default price estimate since we can't see the exact price
        # The VMC will handle insufficient funds
        amount = self.strategy.card_amount(3.00)
        await self.publish(
            client, "payment/credit",
            PaymentEvent(amount=amount, method=method),
        )
        logger.info(f"[mdb] Card/NFC payment: ${amount:.2f} via {method}")

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the MDB gateway simulation."""
        logger.info("[mdb] Starting MDB gateway simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._publish_device_status(client))
            tg.create_task(self._watch_vmc_status(client))
            tg.create_task(self._payment_loop(client))


if __name__ == "__main__":
    args = ESP32Simulator.parse_args()
    sim = MDBGatewaySimulator(broker=args.broker, port=args.port, machine_id=args.machine_id)
    asyncio.run(sim.run())
