# simulators/base.py
"""
Base class for ESP32 simulator processes.

Handles MQTT connection, heartbeat publishing, CLI argument parsing,
and automatic reconnection. Subclasses implement run_simulation().
"""
import argparse
import asyncio
import json
import sys
import time
from abc import ABC, abstractmethod

import aiomqtt
from loguru import logger
from pydantic import BaseModel


class ESP32Simulator(ABC):
    """
    Abstract base for all ESP32 simulators.

    Subclass and implement run_simulation(client) with the device-specific
    behavior. The base class handles MQTT connect, heartbeat, reconnect,
    and single-reader message dispatch.
    """

    HEARTBEAT_INTERVAL = 10.0  # seconds

    def __init__(
        self,
        subsystem_name: str,
        broker: str = "localhost",
        port: int = 1883,
        machine_id: str = "vmc-0000",
    ):
        self.subsystem_name = subsystem_name
        self.broker = broker
        self.port = port
        self.machine_id = machine_id
        self._start_time = time.monotonic()
        self._subscriptions: list[tuple[str, asyncio.Queue]] = []

    @property
    def topic_prefix(self) -> str:
        return f"vmc/{self.machine_id}"

    def _build_heartbeat(self) -> dict:
        uptime = int(time.monotonic() - self._start_time)
        return {
            "subsystem": self.subsystem_name,
            "uptime_seconds": uptime,
        }

    async def _heartbeat_loop(self, client: aiomqtt.Client):
        """Publish heartbeat every HEARTBEAT_INTERVAL seconds."""
        topic = f"{self.topic_prefix}/heartbeat/{self.subsystem_name}"
        while True:
            payload = self._build_heartbeat()
            await client.publish(topic, json.dumps(payload))
            logger.debug(f"[{self.subsystem_name}] heartbeat: uptime={payload['uptime_seconds']}s")
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def publish(self, client: aiomqtt.Client, topic_suffix: str, payload: BaseModel | dict):
        """Publish a message to vmc/{machine_id}/{topic_suffix}."""
        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)
        await client.publish(full_topic, data)
        logger.debug(f"[{self.subsystem_name}] published to {full_topic}")

    async def subscribe(self, client: aiomqtt.Client, topic: str) -> asyncio.Queue:
        """Subscribe to a topic and return a Queue that receives (topic, payload) tuples.

        Uses a single message reader in the base class so multiple subscriptions
        don't fight over ``client.messages``.
        """
        await client.subscribe(topic)
        queue: asyncio.Queue = asyncio.Queue()
        self._subscriptions.append((topic, queue))
        logger.debug(f"[{self.subsystem_name}] Subscribed to {topic}")
        return queue

    async def _message_dispatcher(self, client: aiomqtt.Client):
        """Single reader for ``client.messages``; routes to subscription queues."""
        async for message in client.messages:
            topic_str = str(message.topic)
            try:
                payload = json.loads(message.payload)
            except (json.JSONDecodeError, TypeError):
                continue
            for pattern, queue in self._subscriptions:
                if self._topic_matches(pattern, topic_str):
                    await queue.put((topic_str, payload))

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """Simple MQTT topic matching with + and # wildcards."""
        pat_parts = pattern.split("/")
        top_parts = topic.split("/")
        for i, pat in enumerate(pat_parts):
            if pat == "#":
                return True
            if i >= len(top_parts):
                return False
            if pat != "+" and pat != top_parts[i]:
                return False
        return len(pat_parts) == len(top_parts)

    @abstractmethod
    async def run_simulation(self, client: aiomqtt.Client) -> None:
        """Subclass implements device-specific simulation here."""

    async def run(self) -> None:
        """Main entry: connect, run heartbeat + simulation, reconnect on failure."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.broker,
                    port=self.port,
                    identifier=f"sim-{self.subsystem_name}",
                ) as client:
                    logger.info(
                        f"[{self.subsystem_name}] Connected to {self.broker}:{self.port}"
                    )
                    self._subscriptions.clear()
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._heartbeat_loop(client))
                        tg.create_task(self.run_simulation(client))
                        tg.create_task(self._message_dispatcher(client))
            except aiomqtt.MqttError as e:
                logger.error(f"[{self.subsystem_name}] MQTT error: {e}")
            except Exception as e:
                logger.error(f"[{self.subsystem_name}] Unexpected error: {e}")

            logger.info(f"[{self.subsystem_name}] Reconnecting in 5s...")
            await asyncio.sleep(5)

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments common to all simulators."""
        parser = argparse.ArgumentParser(description="ESP32 Simulator")
        parser.add_argument("--broker", default="localhost", help="MQTT broker host")
        parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
        parser.add_argument("--machine-id", default="vmc-0000", help="Machine ID")
        return parser.parse_args(argv)

    @staticmethod
    def entry_point(simulator_class, **kwargs):
        """Standard entry point: parse args, set Windows event loop policy, and run."""
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        args = ESP32Simulator.parse_args()
        sim = simulator_class(broker=args.broker, port=args.port, machine_id=args.machine_id, **kwargs)
        asyncio.run(sim.run())
