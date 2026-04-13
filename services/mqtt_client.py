# services/mqtt_client.py
"""
Async MQTT client service for the VMC.

Connects to the MQTT broker, subscribes to ESP32 topics, dispatches
incoming messages to registered handlers, and publishes VMC status/commands.
Handles reconnection automatically.
"""
import asyncio
import json
from typing import Any, Callable, Awaitable, Optional

import aiomqtt
from loguru import logger
from pydantic import BaseModel

from config.config_model import MQTTConfig


# Type alias for message handler coroutines
MessageHandler = Callable[[str, dict], Awaitable[None]]

# Type alias for connection-status callback
ConnectionCallback = Callable[[bool], None]


class MQTTClient:
    """
    Async MQTT client that runs as a long-lived asyncio task.

    Usage:
        client = MQTTClient(config=mqtt_config, machine_id="vmc-0001")
        client.register("payment/credit", handle_payment)
        client.register("sensors/temp/+", handle_sensor)
        await client.run()  # blocks, reconnects on failure
    """

    def __init__(self, config: MQTTConfig, machine_id: str):
        self._config = config
        self._machine_id = machine_id
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._client: Optional[aiomqtt.Client] = None
        self._connected = False
        self._connection_callback: Optional[ConnectionCallback] = None

    @property
    def connected(self) -> bool:
        return self._connected

    def set_connection_callback(self, callback: ConnectionCallback):
        """Register a callback invoked with True/False on connect/disconnect."""
        self._connection_callback = callback

    @property
    def topic_prefix(self) -> str:
        return f"vmc/{self._machine_id}"

    def register(self, topic_suffix: str, handler: MessageHandler):
        """
        Register a handler for messages matching a topic suffix.

        The full topic subscribed will be: vmc/{machine_id}/{topic_suffix}
        Supports MQTT wildcards (+ and #).
        """
        self._handlers.append((topic_suffix, handler))
        logger.debug(f"MQTT: Registered handler for {self.topic_prefix}/{topic_suffix}")

    async def publish(self, topic_suffix: str, payload: BaseModel | dict):
        """
        Publish a message to vmc/{machine_id}/{topic_suffix}.

        Accepts either a Pydantic model (serialized to JSON) or a plain dict.
        """
        if self._client is None or not self._connected:
            logger.warning(f"MQTT: Cannot publish to {topic_suffix} — not connected")
            return

        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)

        try:
            await self._client.publish(full_topic, data)
            logger.debug(f"MQTT: Published to {full_topic}")
        except Exception as e:
            logger.error(f"MQTT: Failed to publish to {full_topic}: {e}")

    async def run(self):
        """
        Main loop: connect, subscribe, and dispatch messages.
        Reconnects automatically on disconnection.
        """
        while True:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttError as e:
                self._connected = False
                if self._connection_callback:
                    self._connection_callback(False)
                logger.error(f"MQTT: Connection lost: {e}")
            except Exception as e:
                self._connected = False
                if self._connection_callback:
                    self._connection_callback(False)
                logger.error(f"MQTT: Unexpected error: {e}")

            logger.info(f"MQTT: Reconnecting in {self._config.reconnect_interval}s...")
            await asyncio.sleep(self._config.reconnect_interval)

    async def _connect_and_listen(self):
        """Connect to broker, subscribe to all registered topics, and dispatch."""
        password = None
        if self._config.password is not None:
            password = self._config.password.get_secret_value()

        async with aiomqtt.Client(
            hostname=self._config.broker_host,
            port=self._config.broker_port,
            username=self._config.username,
            password=password,
            identifier=self._config.client_id,
            keepalive=self._config.keepalive,
        ) as client:
            self._client = client
            self._connected = True
            if self._connection_callback:
                self._connection_callback(True)
            logger.info(f"MQTT: Connected to {self._config.broker_host}:{self._config.broker_port}")

            # Subscribe to all registered topic patterns
            for topic_suffix, _ in self._handlers:
                full_topic = f"{self.topic_prefix}/{topic_suffix}"
                await client.subscribe(full_topic)
                logger.info(f"MQTT: Subscribed to {full_topic}")

            # Listen and dispatch
            async for message in client.messages:
                await self._dispatch(message)

        # If we exit the context manager, we disconnected
        self._client = None
        self._connected = False
        if self._connection_callback:
            self._connection_callback(False)

    async def _dispatch(self, message: aiomqtt.Message):
        """Route an incoming message to matching handlers."""
        topic_str = str(message.topic)
        prefix = f"{self.topic_prefix}/"

        if not topic_str.startswith(prefix):
            return

        suffix = topic_str[len(prefix):]

        # Parse payload
        try:
            payload = json.loads(message.payload)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"MQTT: Non-JSON payload on {topic_str}: {message.payload!r}")
            return

        # Match against registered handlers
        for topic_pattern, handler in self._handlers:
            if self._topic_matches(topic_pattern, suffix):
                try:
                    await handler(suffix, payload)
                except Exception as e:
                    logger.error(f"MQTT: Handler error for {topic_str}: {e}")

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """
        Simple MQTT topic matching supporting + (single level) and # (multi level).
        """
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")

        for i, pat in enumerate(pattern_parts):
            if pat == "#":
                return True
            if i >= len(topic_parts):
                return False
            if pat != "+" and pat != topic_parts[i]:
                return False

        return len(pattern_parts) == len(topic_parts)
