"""MQTT client for driving one board (simulator or hardware) in the conformance suite.

One background reader buffers every message under ``vmc/<machine_id>/#`` with a
monotonically growing index, so waits never miss a message that arrived before
they started: ``mark()`` returns the current index and ``since=`` restricts a
wait to messages after it. All waiting is bounded and condition-driven.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid

import aiomqtt

from ice_contracts.board.common import CommandAck

SUBSYSTEM = "vending"

EXPECTED_COMMANDS = {"ping", "self_test", "force_report", "dispense", "water_valve"}
EXPECTED_CHANNEL_IDS = {
    # inputs
    "bag_full_sensor", "button_ice", "button_water_1gal", "button_water_5gal",
    "ro_tank_full", "ro_tank_empty", "water_pressure_ok", "sump_high", "service_door",
    # outputs
    "agitator_motor", "auger_motor", "bag_fan", "trap_door_catch",
    "water_valve_solenoid", "ro_production", "heater_power",
    # analog
    "fill_pulses", "ambient",
}  # fmt: skip


def broker_address() -> tuple[str, int]:
    """host, port from ICE_COLDER_BOARD_BROKER (default localhost:1883)."""
    raw = os.environ.get("ICE_COLDER_BOARD_BROKER") or "localhost:1883"
    host, _, port = raw.partition(":")
    return host, int(port) if port else 1883


def mqtt_auth() -> dict:
    return {
        "username": os.environ.get("MQTT_USERNAME") or None,
        "password": os.environ.get("MQTT_PASSWORD") or None,
    }


def _matches(pattern: str, suffix: str) -> bool:
    want, got = pattern.split("/"), suffix.split("/")
    return len(want) == len(got) and all(w in ("+", g) for w, g in zip(want, got))


class BoardClient:
    def __init__(self, host: str, port: int, machine_id: str):
        self.machine_id = machine_id
        self._prefix = f"vmc/{machine_id}/"
        self._client = aiomqtt.Client(
            hostname=host,
            port=port,
            identifier=f"conf-{uuid.uuid4().hex[:12]}",
            **mqtt_auth(),
        )
        self._messages: list[tuple[str, dict]] = []
        self._cond = asyncio.Condition()
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "BoardClient":
        await self._client.__aenter__()
        await self._client.subscribe(f"{self._prefix}#", qos=1)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        await self._client.__aexit__(*exc)

    async def _read(self) -> None:
        async for msg in self._client.messages:
            topic = str(msg.topic)
            try:
                payload = json.loads(msg.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            async with self._cond:
                self._messages.append((topic[len(self._prefix) :], payload))
                self._cond.notify_all()

    def mark(self) -> int:
        """Index to pass as ``since=`` to see only messages arriving after now."""
        return len(self._messages)

    def _find(self, suffix: str, predicate, since: int) -> dict | None:
        for s, payload in self._messages[since:]:
            if _matches(suffix, s) and predicate(payload):
                return payload
        return None

    def history(self, suffix: str, since: int = 0) -> list[dict]:
        """Every buffered message on *suffix* after index *since*."""
        return [p for s, p in self._messages[since:] if _matches(suffix, s)]

    async def _wait(self, deadline: float) -> bool:
        """Wait for the next message; False once the deadline has passed."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._cond.wait(), remaining)
        return True

    async def wait_for(
        self, suffix: str, predicate, timeout: float, *, since: int = 0
    ) -> dict:
        """First buffered-or-future message on *suffix* satisfying *predicate*."""
        deadline = time.monotonic() + timeout
        async with self._cond:
            while True:
                found = self._find(suffix, predicate, since)
                if found is not None:
                    return found
                if not await self._wait(deadline):
                    raise TimeoutError(f"no matching message on {suffix} in {timeout}s")

    async def retained(self, suffix: str, timeout: float = 5) -> dict:
        return await self.wait_for(suffix, lambda _p: True, timeout)

    async def publish_raw(self, suffix: str, payload: dict) -> None:
        await self._client.publish(self._prefix + suffix, json.dumps(payload), qos=1)

    async def send(
        self, command: str, params: dict | None = None, request_id: str | None = None
    ) -> str:
        request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
        await self.publish_raw(
            f"cmd/{SUBSYSTEM}",
            {"request_id": request_id, "command": command, "params": params or {}},
        )
        return request_id

    async def acks(
        self, request_id: str, count: int, timeout: float, *, since: int = 0
    ) -> list[CommandAck]:
        """Wait until *count* acks for *request_id* exist; return them in order."""
        deadline = time.monotonic() + timeout
        async with self._cond:
            while True:
                got = [
                    CommandAck.model_validate(p)
                    for s, p in self._messages[since:]
                    if s == f"cmd/{SUBSYSTEM}/ack" and p.get("request_id") == request_id
                ]
                if len(got) >= count:
                    return got[:count]
                if not await self._wait(deadline):
                    raise TimeoutError(
                        f"{len(got)}/{count} acks for {request_id} in {timeout}s"
                    )

    async def collect(self, suffix: str, seconds: float) -> list[dict]:
        """Every message on *suffix* arriving during the next *seconds*."""
        start = self.mark()
        deadline = time.monotonic() + seconds
        async with self._cond:
            while await self._wait(deadline):
                pass
        return self.history(suffix, start)
