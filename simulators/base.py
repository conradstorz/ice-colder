# simulators/base.py
"""
Base class for ESP32 simulator processes.

Handles MQTT connection, heartbeat publishing, CLI argument parsing,
and automatic reconnection. Subclasses implement run_simulation().
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import socket
import sys
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

import aiomqtt
from loguru import logger
from pydantic import BaseModel, ValidationError

from config.config_model import ConfigModel
from contracts.common import CommandAck, SubsystemCommand
from contracts.vending_machine import (
    CONTRACT_VERSION as VENDING_CONTRACT_VERSION,
    SubsystemCapabilities,
)
from services.build_info import BUILD_INFO
from services.mqtt_client import PROTOCOL_VERSIONS


RECOVERY_RANGES: dict[str, tuple[float, float]] = {
    "short": (3 * 60, 10 * 60),
    "medium": (10 * 60, 20 * 60),
    "long": (20 * 60, 60 * 60),
}

FAULT_LOOP_INTERVAL = 30.0  # seconds between fault probability rolls
RECOVERY_RETRY_SECONDS = 30.0  # delay before retrying a failed recovery

# §1.1: every subsystem keeps the last 32 acked request_ids so a dispatcher
# retry (same request_id) replays the cached ack instead of repeating the
# handler's side effect.
IDEMPOTENCY_CACHE_SIZE = 32


@dataclass
class FaultDef:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float  # chance per FAULT_LOOP_INTERVAL tick
    on_activate: Callable  # async fn(client) — enter degraded state
    on_recover: Callable  # async fn(client) — restore normal state
    message: str  # human-readable alert text
    severity: str = "warning"  # "warning" | "critical"


@dataclass
class CommandOutcome:
    """What a registered command handler returns to shape its ack.

    Returning ``None`` from a handler is shorthand for ``CommandOutcome()``
    (status "ok", no result); returning a plain ``dict`` is shorthand for
    ``CommandOutcome(result=that_dict)``. Return a ``CommandOutcome``
    directly when the handler needs a non-"ok" status for an expected,
    non-exceptional outcome (e.g. a lockout window answering "rejected") —
    a handler that *raises* is already caught by the command loop and
    acked "failed" with the exception text, so there is no need to catch
    your own exceptions just to report a failure.
    """

    status: Literal["ok", "rejected", "failed", "unsupported"] = "ok"
    detail: str | None = None
    result: dict | None = None


# The registration-hook contract Tasks 7-9 build on: a command handler is an
# async callable taking the connected client and the validated inbound
# command, returning None / a plain result dict / or a CommandOutcome.
CommandHandler = Callable[
    [aiomqtt.Client, SubsystemCommand],
    Awaitable[Union[CommandOutcome, dict, None]],
]


class ESP32Simulator(ABC):
    """
    Abstract base for all ESP32 simulators.

    Subclass and implement run_simulation(client) with the device-specific
    behavior. The base class handles MQTT connect, heartbeat, reconnect,
    and single-reader message dispatch.
    """

    HEARTBEAT_INTERVAL = 10.0  # seconds
    CONTRACT_VERSION = VENDING_CONTRACT_VERSION  # ice maker overrides
    SUPPORTED_COMMANDS: list[str] = []
    BRAND = ""
    MODEL = ""

    def __init__(
        self,
        subsystem_name: str,
        broker: str = "localhost",
        port: int = 1883,
        machine_id: str | None = None,
        config: ConfigModel | None = None,
        config_path: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.subsystem_name = subsystem_name
        self.broker = broker
        self.port = port
        self.config = config or ConfigModel()
        self.machine_id = machine_id or self.config.machine_id
        self._config_path = config_path
        self.username = username
        self.password = password
        self._start_time = time.monotonic()
        self._subscriptions: list[tuple[str, asyncio.Queue]] = []
        self._fault_defs: list[FaultDef] = []
        self._fault_state: dict[str, dict] = {}
        self._recovery_tasks: set[asyncio.Task] = set()
        self._commands: dict[str, CommandHandler] = {}
        self._acked: OrderedDict[str, CommandAck] = OrderedDict()
        self.register_command("ping", self._handle_ping)
        self.register_command("self_test", self._handle_self_test)
        self.register_command("force_report", self._handle_force_report)

    @property
    def topic_prefix(self) -> str:
        return f"vmc/{self.machine_id}"

    def _build_heartbeat(self) -> dict:
        uptime = int(time.monotonic() - self._start_time)
        return {
            "subsystem": self.subsystem_name,
            "uptime_seconds": uptime,
        }

    async def _publish_heartbeat(self, client: aiomqtt.Client) -> None:
        """Publish one heartbeat immediately (shared by the loop and force_report)."""
        topic = f"{self.topic_prefix}/heartbeat/{self.subsystem_name}"
        payload = self._build_heartbeat()
        await client.publish(topic, json.dumps(payload), qos=1)
        logger.debug(
            f"[{self.subsystem_name}] heartbeat: uptime={payload['uptime_seconds']}s"
        )

    async def _heartbeat_loop(self, client: aiomqtt.Client):
        """Publish heartbeat every HEARTBEAT_INTERVAL seconds."""
        while True:
            await self._publish_heartbeat(client)
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

    def fake_hardware_id(self) -> str:
        """Stable locally-administered MAC derived from machine id + subsystem."""
        digest = hashlib.sha1(
            f"{self.machine_id}/{self.subsystem_name}".encode()
        ).digest()
        return "02:" + ":".join(f"{b:02x}" for b in digest[:5])

    @staticmethod
    def container_ip() -> str | None:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None

    def build_capabilities(self) -> SubsystemCapabilities:
        """Retained self-description; subclasses override to add channels etc."""
        return SubsystemCapabilities(
            subsystem=self.subsystem_name,
            firmware=BUILD_INFO.commit_short,
            contract_version=self.CONTRACT_VERSION,
            brand=self.BRAND,
            model=self.MODEL,
            hardware_id=self.fake_hardware_id(),
            ip=self.container_ip(),
            commands=list(self.SUPPORTED_COMMANDS),
        )

    async def _publish_capabilities(self, client: aiomqtt.Client) -> None:
        caps = self.build_capabilities()
        await self.publish(
            client, f"capabilities/{self.subsystem_name}", caps, retain=True
        )
        logger.info(
            f"[{self.subsystem_name}] Capabilities published "
            f"(firmware {caps.firmware}, contract {caps.contract_version})"
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
        """Run a fault's on_recover callback, then clear its state and publish.

        If ``on_recover`` raises, the fault must not get stuck permanently
        "recovering" (which would block both its own future recovery
        attempts and any other fault from ever being rolled/injected).
        Cancellation is re-raised so task cancellation still works normally;
        any other exception is logged and the fault stays active with its
        recovery timer pushed out so ``_check_recoveries`` retries later.
        """
        state = self._fault_state[fault.name]
        try:
            await fault.on_recover(client)
            state["active"] = False
            state["recovering"] = False
            await self._publish_alert(client, fault, "cleared")
            logger.info(f"[{self.subsystem_name}] Fault cleared: {fault.name}")
        except asyncio.CancelledError:
            state["recovering"] = False
            raise
        except Exception as e:
            logger.error(
                f"[{self.subsystem_name}] Recovery failed for fault {fault.name}: {e}"
            )
            state["recovering"] = False
            state["recover_at"] = time.monotonic() + RECOVERY_RETRY_SECONDS

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

    # --- Shared command loop (§1.1, §1.2) -----------------------------------
    #
    # Generic command channel: subscribe to cmd/<subsystem>, dispatch to a
    # registered handler, ack on cmd/<subsystem>/ack. `ping`, `self_test`
    # and `force_report` are registered as ordinary handlers in __init__ —
    # there is nothing special about a "built-in" command versus one a
    # subclass adds with `register_command`; they share one dispatch path,
    # one idempotency cache, and one exception-to-"failed" translation.

    def register_command(self, name: str, handler: CommandHandler) -> None:
        """Register a handler for command `name` on the shared command loop.

        Call from a subclass's __init__ (after `super().__init__()`) to add
        a command beyond the built-in ping/self_test/force_report — this is
        the only extension point subclasses need; `simulators/base.py`
        itself never has to change for a new command.

        `handler` is an async callable ``(client, cmd) -> CommandOutcome |
        dict | None``:
          - `None` acks "ok" with no result;
          - a plain `dict` acks "ok" with that dict as the ack's `result`;
          - a `CommandOutcome` gives full control (a non-"ok" status such as
            "rejected", plus optional `detail`/`result`).
        A handler that raises is caught by the command loop and acked
        "failed" with `str(exception)` as `detail` — handlers never need to
        catch their own exceptions just to report a failure.

        Registering the same `name` twice replaces the earlier handler
        (last registration wins); nothing stops a subclass from overriding
        a built-in this way, though none currently do.
        """
        self._commands[name] = handler

    async def _handle_ping(self, client: aiomqtt.Client, cmd: SubsystemCommand) -> None:
        """§1.2: round trip only — the ack itself is the proof."""
        return None

    async def _handle_self_test(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> dict:
        """§1.2: one check per registered FaultDef, failing the injected one.

        Reads `_active_fault_names` fresh on every call, so the result
        always reflects the fault state *at the moment of the call* — never
        a snapshot taken when the fault was registered or the simulator
        started.
        """
        active = self._active_fault_names
        checks = [
            {
                "name": fault.name,
                "pass": fault.name not in active,
                "detail": fault.message if fault.name in active else "ok",
            }
            for fault in self._fault_defs
        ]
        return {"checks": checks}

    async def _force_report_extra(self, client: aiomqtt.Client) -> None:
        """Override to republish this subsystem's own sensors/channels.

        Part of the `force_report` hook contract (§1.2): the base handler
        republishes the heartbeat itself, then awaits this. Default is a
        no-op — a subsystem with no telemetry of its own needs nothing
        here. A subclass overrides this method directly (it is not a
        `register_command` registration) since it augments the base's own
        `force_report` handler rather than replacing it.
        """
        return None

    async def _handle_force_report(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> None:
        """§1.2: republish the heartbeat, then the subclass's own telemetry."""
        await self._publish_heartbeat(client)
        await self._force_report_extra(client)
        return None

    def _build_ack(
        self, cmd: SubsystemCommand, outcome: CommandOutcome | dict | None
    ) -> CommandAck:
        """Turn a handler's return value into a CommandAck. See CommandOutcome."""
        if outcome is None:
            return CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="ok"
            )
        if isinstance(outcome, CommandOutcome):
            return CommandAck(
                request_id=cmd.request_id,
                command=cmd.command,
                status=outcome.status,
                detail=outcome.detail,
                result=outcome.result,
            )
        if isinstance(outcome, dict):
            return CommandAck(
                request_id=cmd.request_id,
                command=cmd.command,
                status="ok",
                result=outcome,
            )
        raise TypeError(
            f"command handler for {cmd.command!r} returned {type(outcome)!r}; "
            "expected CommandOutcome, dict, or None"
        )

    async def _handle_command(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> None:
        """Dispatch one validated command: idempotency cache, handler, ack.

        A duplicate `request_id` (still in the last IDEMPOTENCY_CACHE_SIZE
        acked) republishes the cached ack verbatim and returns *without*
        looking up or calling the handler — the side effect runs at most
        once per request_id. A handler exception is caught here and turned
        into a "failed" ack instead of propagating (which would otherwise
        silently kill this loop and read to the dispatcher as a 10s
        timeout rather than an immediate, informative failure).
        """
        ack_topic = f"cmd/{self.subsystem_name}/ack"

        cached = self._acked.get(cmd.request_id)
        if cached is not None:
            await self.publish(client, ack_topic, cached)
            logger.info(
                f"[{self.subsystem_name}] Duplicate request_id {cmd.request_id} "
                f"for {cmd.command}: replaying cached ack ({cached.status})"
            )
            return

        handler = self._commands.get(cmd.command)
        if handler is None:
            ack = CommandAck(
                request_id=cmd.request_id, command=cmd.command, status="unsupported"
            )
        else:
            try:
                outcome = await handler(client, cmd)
                ack = self._build_ack(cmd, outcome)
            except Exception as e:
                logger.error(
                    f"[{self.subsystem_name}] Command {cmd.command} "
                    f"({cmd.request_id}) raised: {e}"
                )
                ack = CommandAck(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    status="failed",
                    detail=str(e),
                )

        self._acked[cmd.request_id] = ack
        if len(self._acked) > IDEMPOTENCY_CACHE_SIZE:
            self._acked.popitem(last=False)  # evict oldest, keep most recent N
        await self.publish(client, ack_topic, ack)
        logger.info(
            f"[{self.subsystem_name}] Command {cmd.command} ({cmd.request_id}): "
            f"{ack.status}"
        )

    async def _handle_invalid_command(
        self, client: aiomqtt.Client, data, error: ValidationError
    ) -> None:
        """A raw payload that failed `SubsystemCommand.model_validate` — most
        commonly an out-of-range param such as `water_valve`'s `seconds` or
        `power_cycle`'s `dwell_seconds` (`COMMAND_PARAM_VALIDATORS`, enforced
        by the model's own `model_validator` *before* an instance exists).

        Previously this was a silent drop: the dispatcher would wait out the
        full `ACK_TIMEOUT_SECONDS`, retry, wait again, then raise
        `CommandTimeout` — where the operator should have seen an immediate
        "rejected" with the validation message (spec §6, §1.1).

        Publishing that ack requires a `request_id` to correlate it to —
        which lives only in the raw, not-yet-validated payload. When the
        payload gives us a usable one (a non-empty string under
        `"request_id"`), we ack "rejected" with the validation message as
        `detail`, using the payload's own `"command"` value when present
        (falling back to `"unknown"` when it is missing or not a string —
        there is no other reasonable label). When there is nothing usable
        to correlate to — unparseable JSON reaching here as a non-dict,
        no `"request_id"` key, or a `"request_id"` that is not a non-empty
        string — there is no ack to address, so this falls back to the
        original log-and-drop.

        Deliberately *not* written into `self._acked`: that cache exists so
        a dispatcher retry (same `request_id`) replays a handler's *side
        effect* instead of repeating it. A validation rejection never ran a
        handler and has no side effect to protect against — it is a pure
        function of the payload, so a retry with the same `request_id` just
        re-validates (cheap) and gets the same "rejected" ack again. Caching
        it would only add bookkeeping (and cross-command `request_id` cache
        pressure) for no behavioural benefit.
        """
        logger.warning(f"[{self.subsystem_name}] Invalid command dropped: {error}")
        request_id = data.get("request_id") if isinstance(data, dict) else None
        if not isinstance(request_id, str) or not request_id:
            return
        command = data.get("command") if isinstance(data, dict) else None
        if not isinstance(command, str) or not command:
            command = "unknown"
        ack = CommandAck(
            request_id=request_id,
            command=command,
            status="rejected",
            detail=str(error),
        )
        await self.publish(client, f"cmd/{self.subsystem_name}/ack", ack)
        logger.info(
            f"[{self.subsystem_name}] Command {command} ({request_id}): "
            "rejected (validation)"
        )

    async def _command_loop(self, client: aiomqtt.Client) -> None:
        """Subscribe to cmd/<subsystem> and dispatch each command as it arrives.

        Call this from a subclass's `run_simulation` (inside its
        TaskGroup) — the same way the ice-maker simulator wires its own
        command loop today. It is not started automatically from `run()`:
        a subclass registers its own commands via `register_command`
        during `__init__`, and not every simulator has a command channel
        (yet), so `run()` staying agnostic keeps this additive.
        """
        topic = f"{self.topic_prefix}/cmd/{self.subsystem_name}"
        queue = await self.subscribe(client, topic)
        logger.info(f"[{self.subsystem_name}] Command loop started: {topic}")
        while True:
            _, data = await queue.get()
            try:
                cmd = SubsystemCommand.model_validate(data)
            except ValidationError as e:
                await self._handle_invalid_command(client, data, e)
                continue
            await self._handle_command(client, cmd)

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
                    username=self.username,
                    password=self.password,
                    protocol=PROTOCOL_VERSIONS[self.config.mqtt.protocol_version],
                ) as client:
                    logger.info(
                        f"[{self.subsystem_name}] Connected to {self.broker}:{self.port}"
                    )
                    self._subscriptions.clear()
                    await self._publish_ha_discovery(client)
                    await self._publish_capabilities(client)
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
    def credentials_from(config: ConfigModel) -> tuple[str | None, str | None]:
        """Broker credentials: env vars win over config; SecretStr is unwrapped."""
        username = os.environ.get("MQTT_USERNAME") or config.mqtt.username
        password = os.environ.get("MQTT_PASSWORD")
        if not password and config.mqtt.password is not None:
            password = config.mqtt.password.get_secret_value()
        return (username or None, password or None)

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
        username, password = ESP32Simulator.credentials_from(config)
        sim = simulator_class(
            broker=args.broker or config.mqtt.broker_host,
            port=args.port or config.mqtt.broker_port,
            machine_id=args.machine_id,
            config=config,
            config_path=args.config,
            username=username,
            password=password,
            **kwargs,
        )
        asyncio.run(sim.run())
