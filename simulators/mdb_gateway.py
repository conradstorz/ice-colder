# simulators/mdb_gateway.py
"""
MDB payment gateway simulator.

Simulates MDB bus devices (coin acceptor, bill validator, card reader).
Watches VMC status via MQTT and reactively inserts payments when a
customer interaction is detected.

Run: uv run python -m simulators.mdb_gateway [--broker HOST] [--port PORT] [--machine-id ID]
"""
import asyncio
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
        # Build a lookup of product name -> price from config
        self._product_prices = {p.name: p.price for p in self.config.products}
        self._vmc_status: asyncio.Queue = asyncio.Queue()

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for MDB payment devices."""
        entities = []
        for device in self.devices:
            name = device["name"]
            display_name = name.replace("_", " ").title()
            entities.append({
                "component": "binary_sensor",
                "object_id": name,
                "name": f"MDB {display_name}",
                "state_topic_suffix": "payment/status",
                "value_template": f"{{% if value_json.device == '{name}' %}}{{% if value_json.state == 'ready' %}}ON{{% else %}}OFF{{% endif %}}{{% endif %}}",
                "device_class": "running",
                "payload_on": "ON",
                "payload_off": "OFF",
            })
        entities.append({
            "component": "sensor",
            "object_id": "uptime",
            "name": "MDB Gateway Uptime",
            "state_topic_suffix": "heartbeat/mdb",
            "value_template": "{{ value_json.uptime_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "total_increasing",
        })
        return entities

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
        """Read VMC status from the subscription queue and forward to the payment loop."""
        topic = f"{self.topic_prefix}/status"
        status_queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for VMC status on {topic}")
        while True:
            _topic, data = await status_queue.get()
            await self._vmc_status.put(data)

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

            price = self._product_prices.get(selected, 3.00)
            logger.info(f"[mdb] Customer interaction detected, product: {selected} (${price:.2f})")

            # Simulate customer reaching for wallet
            await asyncio.sleep(random.uniform(2.0, 5.0))

            method = self.strategy.pick_method()
            logger.info(f"[mdb] Payment method: {method}")

            if method in ("card", "nfc"):
                await self._do_card_payment(client, method, price=price)
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

    async def _do_card_payment(self, client: aiomqtt.Client, method: str, price: float = 3.00):
        """Insert a card/NFC payment — single transaction."""
        amount = self.strategy.card_amount(price)
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
    ESP32Simulator.entry_point(MDBGatewaySimulator)
