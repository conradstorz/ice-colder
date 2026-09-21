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
from collections import OrderedDict

import aiomqtt
from loguru import logger
from pydantic import ValidationError

from simulators.base import ESP32Simulator, FaultDef
from services.mqtt_messages import PaymentEnableCommand, PaymentEvent, PaymentStatus
from contracts.vending_machine import (
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
)


class PaymentStrategy:
    """Encapsulates the randomized payment logic."""

    COIN_DENOMS = [0.25, 0.50, 1.00]
    BILL_DENOMS = [1.00, 5.00, 10.00, 20.00]
    METHODS = ["cash_coin", "cash_bill", "card", "nfc"]

    def pick_method(self, excluded: set[str] | None = None) -> str | None:
        """Pick a payment method, excluding any in the excluded set."""
        available = [m for m in self.METHODS if m not in (excluded or set())]
        if not available:
            return None
        return random.choice(available)

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
    REFUND_DELAY_RANGE = (0.5, 2.0)  # seconds the changer takes to pay out
    REFUND_RESULTS_MAX = 256  # idempotency cache bound, oldest evicted first
    SUPPORTED_COMMANDS = ["payment/enable", "refund"]
    BRAND = "ice-colder"
    MODEL = "mdb-sim"

    def __init__(self, **kwargs):
        super().__init__(subsystem_name="mdb", **kwargs)
        self.strategy = PaymentStrategy()
        # Real MDB peripherals stay inhibited until the VMC enables them.
        self.accepting = False
        self.devices = [
            {"name": "coin_acceptor", "state": "ready"},
            {"name": "bill_validator", "state": "ready"},
            {"name": "card_reader", "state": "ready"},
        ]
        # Build a lookup of product name -> price from config
        self._product_prices = {p.name: p.price for p in self.config.products}
        self._vmc_status: asyncio.Queue = asyncio.Queue()
        # request_id -> result, so a repeated refund command is never paid twice
        self._refund_results: OrderedDict[str, PaymentRefundResult] = OrderedDict()

        # Register faults
        self.register_fault(
            FaultDef(
                name="coin_acceptor_jammed",
                category="short",
                probability=0.0015,
                on_activate=self._on_coin_acceptor_jammed_activate,
                on_recover=self._on_coin_acceptor_jammed_recover,
                message="Coin acceptor jammed — coins rejected",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="bill_validator_offline",
                category="short",
                probability=0.0012,
                on_activate=self._on_bill_validator_offline_activate,
                on_recover=self._on_bill_validator_offline_recover,
                message="Bill validator offline — bills not accepted",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="card_reader_error",
                category="short",
                probability=0.001,
                on_activate=self._on_card_reader_error_activate,
                on_recover=self._on_card_reader_error_recover,
                message="Card reader error — card and NFC payments unavailable",
                severity="warning",
            )
        )
        self.register_fault(
            FaultDef(
                name="mdb_bus_reset",
                category="medium",
                probability=0.0004,
                on_activate=self._on_mdb_bus_reset_activate,
                on_recover=self._on_mdb_bus_reset_recover,
                message="MDB bus reset — all payment devices temporarily offline",
                severity="critical",
            )
        )
        self.register_fault(
            FaultDef(
                name="changer_empty",
                category="medium",
                probability=0.0003,
                on_activate=self._on_changer_empty_activate,
                on_recover=self._on_changer_empty_recover,
                message="Coin changer empty — refunds cannot be paid out",
                severity="warning",
            )
        )

    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for MDB payment devices."""
        entities = []
        for device in self.devices:
            name = device["name"]
            display_name = name.replace("_", " ").title()
            entities.append(
                {
                    "component": "binary_sensor",
                    "object_id": name,
                    "name": f"MDB {display_name}",
                    "state_topic_suffix": "payment/status",
                    "value_template": f"{{% if value_json.device == '{name}' %}}{{% if value_json.state == 'ready' %}}ON{{% else %}}OFF{{% endif %}}{{% endif %}}",
                    "device_class": "running",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                }
            )
        entities.append(
            {
                "component": "sensor",
                "object_id": "uptime",
                "name": "MDB Gateway Uptime",
                "state_topic_suffix": "heartbeat/mdb",
                "value_template": "{{ value_json.uptime_seconds }}",
                "device_class": "duration",
                "unit_of_measurement": "s",
                "state_class": "total_increasing",
            }
        )
        return entities

    async def _publish_device_status(self, client: aiomqtt.Client):
        """Periodically publish device readiness status."""
        while True:
            for device in self.devices:
                await self.publish(
                    client,
                    "payment/status",
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

    async def _apply_enable(self, data: dict) -> None:
        try:
            cmd = PaymentEnableCommand.model_validate(data)
        except ValidationError as e:
            logger.error(f"[mdb] Bad payment/enable ignored: {e}")
            return
        if cmd.accept != self.accepting:
            logger.info(
                f"[mdb] Payment {'ENABLED' if cmd.accept else 'INHIBITED'} by VMC"
            )
        self.accepting = cmd.accept

    async def _enable_loop(self, client: aiomqtt.Client):
        """Track cmd/payment/enable from the VMC."""
        topic = f"{self.topic_prefix}/cmd/payment/enable"
        queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for payment enable on {topic}")
        while True:
            _topic, data = await queue.get()
            await self._apply_enable(data)

    async def _payment_loop(self, client: aiomqtt.Client):
        """React to VMC state changes by inserting payments."""
        while True:
            status = await self._vmc_status.get()
            state = status.get("state", "")

            if state != "interacting_with_user":
                continue

            if not self.accepting:
                logger.debug("[mdb] Interaction seen but payment inhibited; waiting")
                continue

            selected = status.get("selected_product")
            if not selected:
                continue

            price = self._product_prices.get(selected, 3.00)
            logger.info(
                f"[mdb] Customer interaction detected, product: {selected} (${price:.2f})"
            )

            # Simulate customer reaching for wallet
            await asyncio.sleep(random.uniform(2.0, 5.0))

            excluded = self._build_payment_exclusions()
            method = self.strategy.pick_method(excluded=excluded)
            if method is None:
                logger.info(
                    "[mdb] No payment methods available (all excluded by faults)"
                )
                continue

            logger.info(f"[mdb] Payment method: {method}")

            if method in ("card", "nfc"):
                await self._do_card_payment(client, method, price=price)
            else:
                await self._do_cash_payment(client, method)

    async def _do_cash_payment(self, client: aiomqtt.Client, method: str):
        """Insert cash denominations, possibly requiring multiple attempts."""
        for attempt in range(self.MAX_CASH_ATTEMPTS):
            if not self.accepting:
                logger.info("[mdb] Payment inhibited; cash rejected")
                return

            if method == "cash_coin":
                amount = self.strategy.pick_coin()
            else:
                amount = self.strategy.pick_bill()

            await self.publish(
                client,
                "payment/credit",
                PaymentEvent(amount=amount, method=method),
            )
            logger.info(
                f"[mdb] Inserted ${amount:.2f} via {method} (attempt {attempt + 1})"
            )

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

    async def _do_card_payment(
        self, client: aiomqtt.Client, method: str, price: float = 3.00
    ):
        """Insert a card/NFC payment — single transaction."""
        if not self.accepting:
            logger.info("[mdb] Payment inhibited; card not accepted")
            return
        amount = self.strategy.card_amount(price)
        await self.publish(
            client,
            "payment/credit",
            PaymentEvent(amount=amount, method=method),
        )
        logger.info(f"[mdb] Card/NFC payment: ${amount:.2f} via {method}")

    def _build_payment_exclusions(self) -> set[str]:
        """Return the set of payment methods excluded by currently active faults."""
        active = self._active_fault_names
        excluded: set[str] = set()
        if "coin_acceptor_jammed" in active:
            excluded.add("cash_coin")
        if "bill_validator_offline" in active:
            excluded.add("cash_bill")
        if "card_reader_error" in active:
            excluded.update({"card", "nfc"})
        if "mdb_bus_reset" in active:
            excluded.update({"cash_coin", "cash_bill", "card", "nfc"})
        return excluded

    def _device_by_name(self, name: str) -> dict:
        device = next((d for d in self.devices if d["name"] == name), None)
        if device is None:
            raise ValueError(f"[mdb] Unknown device: {name!r}")
        return device

    async def _set_device_state(
        self, client: aiomqtt.Client, name: str, state: str
    ) -> None:
        device = self._device_by_name(name)
        device["state"] = state
        await self.publish(
            client, "payment/status", PaymentStatus(device=name, state=state)
        )

    async def _on_coin_acceptor_jammed_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "coin_acceptor", "error")
        logger.warning("[mdb] FAULT: coin acceptor jammed")

    async def _on_coin_acceptor_jammed_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "coin_acceptor", "ready")
        logger.info("[mdb] Fault cleared: coin_acceptor_jammed")

    async def _on_bill_validator_offline_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "bill_validator", "offline")
        logger.warning("[mdb] FAULT: bill validator offline")

    async def _on_bill_validator_offline_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "bill_validator", "ready")
        logger.info("[mdb] Fault cleared: bill_validator_offline")

    async def _on_card_reader_error_activate(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "card_reader", "error")
        logger.warning("[mdb] FAULT: card reader error")

    async def _on_card_reader_error_recover(self, client: aiomqtt.Client) -> None:
        await self._set_device_state(client, "card_reader", "ready")
        logger.info("[mdb] Fault cleared: card_reader_error")

    async def _on_mdb_bus_reset_activate(self, client: aiomqtt.Client) -> None:
        for device in self.devices:
            await self._set_device_state(client, device["name"], "offline")
        logger.warning("[mdb] FAULT: MDB bus reset — all devices offline")

    async def _on_mdb_bus_reset_recover(self, client: aiomqtt.Client) -> None:
        for device in self.devices:
            await asyncio.sleep(random.uniform(10.0, 30.0))
            await self._set_device_state(client, device["name"], "ready")
            logger.info(f"[mdb] Device restored: {device['name']}")
        logger.info("[mdb] Fault cleared: mdb_bus_reset")

    async def _on_changer_empty_activate(self, client: aiomqtt.Client) -> None:
        logger.warning("[mdb] FAULT: changer empty — refunds will fail")

    async def _on_changer_empty_recover(self, client: aiomqtt.Client) -> None:
        logger.info("[mdb] Fault cleared: changer_empty")

    async def _refund_loop(self, client: aiomqtt.Client):
        """Answer VMC refund commands from the subscription queue."""
        topic = f"{self.topic_prefix}/cmd/payment/refund"
        queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for refund commands on {topic}")
        while True:
            _topic, data = await queue.get()
            try:
                cmd = PaymentRefundCommand.model_validate(data)
            except ValidationError as e:
                logger.error(f"[mdb] Bad refund command ignored: {e}")
                continue
            await self._handle_refund(client, cmd)

    async def _handle_refund(
        self, client: aiomqtt.Client, cmd: PaymentRefundCommand
    ) -> None:
        cached = self._refund_results.get(cmd.request_id)
        if cached is not None:
            logger.info(
                f"[mdb] Refund {cmd.request_id}: repeat request, re-sending result"
            )
            await self.publish(client, "cmd/payment/refund/ack", cached)
            return

        await asyncio.sleep(random.uniform(*self.REFUND_DELAY_RANGE))
        if "changer_empty" in self._active_fault_names:
            result = PaymentRefundResult(
                request_id=cmd.request_id,
                status=RefundStatus.failed,
                amount_returned=0.0,
                detail="changer_empty",
            )
            logger.warning(
                f"[mdb] Refund {cmd.request_id}: FAILED, changer empty (${cmd.amount:.2f})"
            )
        else:
            result = PaymentRefundResult(
                request_id=cmd.request_id,
                status=RefundStatus.ok,
                amount_returned=cmd.amount,
            )
            logger.info(
                f"[mdb] Refund {cmd.request_id}: paid out ${cmd.amount:.2f} ({cmd.reason})"
            )
        self._refund_results[cmd.request_id] = result
        while len(self._refund_results) > self.REFUND_RESULTS_MAX:
            self._refund_results.popitem(last=False)
        await self.publish(client, "cmd/payment/refund/ack", result)

    async def run_simulation(self, client: aiomqtt.Client):
        """Run the MDB gateway simulation."""
        logger.info("[mdb] Starting MDB gateway simulation")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._publish_device_status(client))
            tg.create_task(self._watch_vmc_status(client))
            tg.create_task(self._payment_loop(client))
            tg.create_task(self._refund_loop(client))
            tg.create_task(self._enable_loop(client))


if __name__ == "__main__":
    ESP32Simulator.entry_point(MDBGatewaySimulator)
