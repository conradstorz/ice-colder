# simulators/base.py
"""
Base class for ESP32 simulator processes.

Handles MQTT connection, heartbeat publishing, CLI argument parsing,
and automatic reconnection. Subclasses implement run_simulation().
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiomqtt
from loguru import logger
from pydantic import BaseModel, ValidationError

from config.config_model import ConfigModel


RECOVERY_RANGES: dict[str, tuple[float, float]] = {
    "short": (3 * 60, 10 * 60),
    "medium": (10 * 60, 20 * 60),
    "long": (20 * 60, 60 * 60),
}

FAULT_LOOP_INTERVAL = 30.0  # seconds between fault probability rolls


@dataclass
class FaultDef:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float  # chance per FAULT_LOOP_INTERVAL tick
    on_activate: Callable  # async fn(client) — enter degraded state
    on_recover: Callable  # async fn(client) — restore normal state
    message: str  # human-readable alert text
    severity: str = "warning"  # "warning" | "critical"


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
        self._fault_defs: list[FaultDef] = []
        self._fault_state: dict[str, dict] = {}
        self._recovery_tasks: set[asyncio.Task] = set()

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
            await client.publish(topic, json.dumps(payload), qos=1)
            logger.debug(
                f"[{self.subsystem_name}] heartbeat: uptime={payload['uptime_seconds']}s"
            )
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    def _build_will(self) -> aiomqtt.Will:
        """LWT: mark this subsystem offline instantly on unclean disconnect."""
        return aiomqtt.Will(
            topic=f"{self.topic_prefix}/heartbeat/{self.subsystem_name}",
            payload=json.dumps(
                {"subsystem": self.subsystem_name, "uptime_seconds": -1}
            ),
            qos=1,
        )

    async def publish(
        self,
        client: aiomqtt.Client,
        topic_suffix: str,
        payload: BaseModel | dict,
        retain: bool = False,
        qos: int = 1,
    ):
        """Publish a message to vmc/{machine_id}/{topic_suffix}."""
        full_topic = f"{self.topic_prefix}/{topic_suffix}"
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        else:
            data = json.dumps(payload)
        await client.publish(full_topic, data, qos=qos, retain=retain)
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
        """Single reader for ``client.messages``; routes to subscription queues.

        Subscribes to a keepalive topic so aiomqtt's message loop stays active
        even when the simulator has no application-level subscriptions.
        """
        await client.subscribe(f"{self.topic_prefix}/noop")
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

    def register_fault(self, fault: FaultDef) -> None:
        """Register a fault definition. Call from subclass __init__."""
        self._fault_defs.append(fault)
        self._fault_state[fault.name] = {
            "active": False,
            "recover_at": 0.0,
            "recovering": False,
        }

    @property
    def _active_fault_names(self) -> set[str]:
        """Return the set of currently active fault names."""
        return {name for name, state in self._fault_state.items() if state["active"]}

    async def _activate_fault(
        self,
        client: aiomqtt.Client,
        fault: FaultDef,
        recover_in: float | None = None,
    ) -> None:
        """Activate a fault: set state, call on_activate, publish alert."""
        if recover_in is None:
            lo, hi = RECOVERY_RANGES[fault.category]
            recover_in = random.uniform(lo, hi)
        self._fault_state[fault.name]["active"] = True
        self._fault_state[fault.name]["recover_at"] = time.monotonic() + recover_in
        await fault.on_activate(client)
        await self._publish_alert(client, fault, "active", recover_in=recover_in)
        logger.warning(
            f"[{self.subsystem_name}] Fault activated: {fault.name} "
            f"(recover in {recover_in / 60:.1f} min)"
        )

    async def _check_recoveries(self, client: aiomqtt.Client) -> None:
        """Start recovery for any faults whose recovery timer has elapsed.

        Recovery (``fault.on_recover``) runs in a background task rather than
        being awaited inline: some recoveries are slow (e.g. restoring
        several devices with per-device delays), and awaiting them here would
        block this single ``_fault_loop`` from draining inject commands or
        rolling other faults for the whole recovery duration. The fault is
        marked "recovering" and stays counted as active (via
        ``_active_fault_names``) until the background task actually
        completes, so callers that gate behavior on active faults keep
        excluding it the whole time.
        """
        now = time.monotonic()
        for fault in self._fault_defs:
            state = self._fault_state[fault.name]
            if (
                state["active"]
                and not state.get("recovering")
                and now >= state["recover_at"]
            ):
                state["recovering"] = True
                task = asyncio.create_task(self._run_recovery(client, fault))
                self._recovery_tasks.add(task)
                task.add_done_callback(self._recovery_tasks.discard)

    async def _run_recovery(self, client: aiomqtt.Client, fault: FaultDef) -> None:
        """Run a fault's on_recover callback, then clear its state and publish."""
        await fault.on_recover(client)
        state = self._fault_state[fault.name]
        state["active"] = False
        state["recovering"] = False
        await self._publish_alert(client, fault, "cleared")
        logger.info(f"[{self.subsystem_name}] Fault cleared: {fault.name}")

    def _cancel_recovery_tasks(self) -> None:
        """Cancel any outstanding recovery tasks (call on disconnect/shutdown)."""
        for task in list(self._recovery_tasks):
            task.cancel()
        self._recovery_tasks.clear()

    async def _try_roll_faults(self, client: aiomqtt.Client) -> None:
        """Roll for new faults if none are currently active."""
        if any(s["active"] for s in self._fault_state.values()):
            return
        for fault in self._fault_defs:
            if random.random() < fault.probability:
                await self._activate_fault(client, fault)
                break  # one fault at a time

    async def _publish_alert(
        self,
        client: aiomqtt.Client,
        fault: FaultDef,
        status: str,
        recover_in: float | None = None,
    ) -> None:
        """Publish a structured alert to vmc/{machine_id}/alert/{subsystem}."""
        topic = f"{self.topic_prefix}/alert/{self.subsystem_name}"
        payload: dict = {
            "subsystem": self.subsystem_name,
            "fault": fault.name,
            "status": status,
            "message": fault.message,
            "severity": fault.severity,
        }
        if recover_in is not None:
            payload["recover_in_seconds"] = int(recover_in)
        await client.publish(topic, json.dumps(payload))
        logger.debug(f"[{self.subsystem_name}] Alert: {fault.name} {status}")

    async def _handle_inject_command(self, client: aiomqtt.Client, data: dict) -> None:
        """Process a manual fault inject command payload."""
        name = data.get("fault")
        if not name:
            return
        fault = next((f for f in self._fault_defs if f.name == name), None)
        if not fault:
            logger.warning(f"[{self.subsystem_name}] Inject: unknown fault '{name}'")
            return
        if any(s["active"] for s in self._fault_state.values()):
            logger.info(
                f"[{self.subsystem_name}] Inject ignored: a fault is already active"
            )
            return
        await self._activate_fault(client, fault)
        logger.info(f"[{self.subsystem_name}] Fault injected: {name}")

    async def _fault_loop(self, client: aiomqtt.Client) -> None:
        """Periodic task: drain inject commands, check recoveries, roll for new faults."""
        inject_topic = f"{self.topic_prefix}/cmd/sim/inject_fault"
        inject_queue = await self.subscribe(client, inject_topic)
        logger.info(
            f"[{self.subsystem_name}] Fault loop started, inject topic: {inject_topic}"
        )
        while True:
            await asyncio.sleep(FAULT_LOOP_INTERVAL)
            # Drain inject commands first
            while not inject_queue.empty():
                _, data = inject_queue.get_nowait()
                await self._handle_inject_command(client, data)
            # Check if any active faults have recovered
            await self._check_recoveries(client)
            # Roll for new faults
            await self._try_roll_faults(client)

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
                "device_class",
                "unit_of_measurement",
                "state_class",
                "payload_on",
                "payload_off",
                "expire_after",
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
                    will=self._build_will(),
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
                        tg.create_task(self._fault_loop(client))
            except aiomqtt.MqttError as e:
                logger.error(f"[{self.subsystem_name}] MQTT error: {e}")
            except Exception as e:
                logger.error(f"[{self.subsystem_name}] Unexpected error: {e}")
                if hasattr(e, "exceptions"):
                    for sub_exc in e.exceptions:
                        logger.error(
                            f"[{self.subsystem_name}]   Sub-exception: {sub_exc!r}"
                        )
            finally:
                # A recovery task holds the old (now-disconnected) client;
                # don't let it linger into the next connection's lifetime.
                self._cancel_recovery_tasks()

            logger.info(f"[{self.subsystem_name}] Reconnecting in 5s...")
            await asyncio.sleep(5)

    @staticmethod
    def load_config(path: str | None = None) -> ConfigModel:
        """Load ConfigModel from JSON file, falling back to defaults.

        When ``path`` is None, honors ``ICE_COLDER_CONFIG`` (read at call
        time, not import time) the same way main.py/config_store.py do,
        defaulting to ``config.json``. Simulators must never crash-loop on a
        bad config: a missing file, a directory at the path, unreadable/
        invalid JSON, or a failed schema validation are all logged and fall
        back to ``ConfigModel()`` defaults rather than raising.
        """
        resolved = (
            path
            if path is not None
            else os.environ.get("ICE_COLDER_CONFIG", "config.json")
        )
        config_path = Path(resolved)

        if not config_path.exists():
            logger.warning(f"{resolved} not found, using default config")
            return ConfigModel()

        if config_path.is_dir():
            logger.error(
                f"Config path '{resolved}' is a directory, not a file; "
                "using default config"
            )
            return ConfigModel()

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            config = ConfigModel.model_validate(raw)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
        ) as e:
            logger.error(
                f"Failed to load config from '{resolved}': {e}; using default config"
            )
            return ConfigModel()

        logger.info(
            f"Simulator loaded config from {resolved}: machine_id={config.machine_id}"
        )
        return config

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments common to all simulators."""
        parser = argparse.ArgumentParser(description="ESP32 Simulator")
        parser.add_argument(
            "--config",
            default=None,
            help="Path to config.json (default: $ICE_COLDER_CONFIG or config.json)",
        )
        parser.add_argument(
            "--broker", default=None, help="MQTT broker host (overrides config)"
        )
        parser.add_argument(
            "--port", type=int, default=None, help="MQTT broker port (overrides config)"
        )
        parser.add_argument(
            "--machine-id", default=None, help="Machine ID (overrides config)"
        )
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
