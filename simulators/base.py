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
from pathlib import Path

import aiomqtt
from loguru import logger
from pydantic import BaseModel

from config.config_model import ConfigModel


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
        machine_id: str | None = None,
        config: ConfigModel | None = None,
    ):
        self.subsystem_name = subsystem_name
        self.broker = broker
        self.port = port
        self.config = config or ConfigModel()
        self.machine_id = machine_id or self.config.machine_id
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

    def ha_discovery_entities(self) -> list[dict]:
        """Override in subclasses to return HA discovery entity definitions.

        Each dict should have keys: component, object_id, name, state_topic_suffix,
        value_template. Optional: device_class, unit_of_measurement, state_class,
        payload_on, payload_off, expire_after.
        """
        return []

    def _build_ha_device(self) -> dict:
        """Build the HA device block shared by all entities from this simulator."""
        subsystem_display = self.subsystem_name.replace("_", " ").title()
        machine_name = self.config.physical.common_name
        return {
            "identifiers": [f"{self.machine_id}_{self.subsystem_name}"],
            "name": f"{machine_name} {subsystem_display}",
            "manufacturer": "ice-colder",
            "model": f"ESP32 {self.subsystem_name} simulator",
            "via_device": self.machine_id,
        }

    async def _publish_ha_discovery(self, client: aiomqtt.Client):
        """Publish HA MQTT auto-discovery config for all entities."""
        entities = self.ha_discovery_entities()
        if not entities:
            return
        device = self._build_ha_device()
        node_id = f"{self.machine_id}_{self.subsystem_name}"
        for entity in entities:
            component = entity["component"]
            object_id = entity["object_id"]
            topic = f"homeassistant/{component}/{node_id}/{object_id}/config"
            payload = {
                "name": entity["name"],
                "unique_id": f"{node_id}_{object_id}",
                "state_topic": f"{self.topic_prefix}/{entity['state_topic_suffix']}",
                "value_template": entity["value_template"],
                "device": device,
            }
            for optional_key in (
                "device_class", "unit_of_measurement", "state_class",
                "payload_on", "payload_off", "expire_after",
            ):
                if optional_key in entity:
                    payload[optional_key] = entity[optional_key]
            await client.publish(topic, json.dumps(payload), retain=True)
            logger.debug(f"[{self.subsystem_name}] HA discovery: {topic}")

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
                    await self._publish_ha_discovery(client)
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
    def load_config(path: str = "config.json") -> ConfigModel:
        """Load ConfigModel from JSON file, falling back to defaults."""
        config_path = Path(path)
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            config = ConfigModel.model_validate(raw)
            logger.info(f"Simulator loaded config from {path}: machine_id={config.machine_id}")
            return config
        logger.warning(f"{path} not found, using default config")
        return ConfigModel()

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments common to all simulators."""
        parser = argparse.ArgumentParser(description="ESP32 Simulator")
        parser.add_argument("--config", default="config.json", help="Path to config.json")
        parser.add_argument("--broker", default=None, help="MQTT broker host (overrides config)")
        parser.add_argument("--port", type=int, default=None, help="MQTT broker port (overrides config)")
        parser.add_argument("--machine-id", default=None, help="Machine ID (overrides config)")
        return parser.parse_args(argv)

    @staticmethod
    def entry_point(simulator_class, **kwargs):
        """Standard entry point: parse args, load config, set Windows event loop policy, and run."""
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        args = ESP32Simulator.parse_args()
        config = ESP32Simulator.load_config(args.config)
        sim = simulator_class(
            broker=args.broker or config.mqtt.broker_host,
            port=args.port or config.mqtt.broker_port,
            machine_id=args.machine_id,
            config=config,
            **kwargs,
        )
        asyncio.run(sim.run())
