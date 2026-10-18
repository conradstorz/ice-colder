"""Opt-in board conformance suite: drives a vending board over MQTT.

Targets (``ICE_COLDER_BOARD_BROKER`` = ``host[:port]``, default ``localhost:1883``):
- ``ICE_COLDER_BOARD_MACHINE_ID`` set: hardware target on that broker/machine.
- unset: simulator target; the vending simulator runs in-process (own thread
  and event loop, package-long) on that broker with a fresh ``machine_id``. It is
  package-scoped so it disconnects before other test packages start their own
  ``sim-vending`` (the client id is fixed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
import pytest

from ice_sims.vending.simulator import VendingMachineSimulator
from tests.firmware.board_client import BoardClient, broker_address, mqtt_auth

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

OPT_IN_REASON = "board conformance tests are opt-in (set ICE_COLDER_BOARD_TESTS=1)"
SIM_ONLY_REASON = "needs the simulator to drive sensors"
_HERE = Path(__file__).parent


def _is_sim() -> bool:
    return not os.environ.get("ICE_COLDER_BOARD_MACHINE_ID")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_sim: needs the simulator to drive sensors or inspect it"
    )


def pytest_collection_modifyitems(config, items):
    enabled = os.environ.get("ICE_COLDER_BOARD_TESTS") == "1"
    for item in items:
        path = Path(str(item.fspath))
        if _HERE not in path.parents:
            continue
        if not enabled and path.name != "test_golden_wire.py":
            item.add_marker(pytest.mark.skip(reason=OPT_IN_REASON))
        elif item.get_closest_marker("requires_sim") and not _is_sim():
            item.add_marker(pytest.mark.skip(reason=SIM_ONLY_REASON))


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    machine_id: str
    is_sim: bool
    client_id: str | None = None


async def _clear_retained(host: str, port: int, machine_id: str) -> None:
    """Remove the retained messages the simulator left behind for this id."""
    topics: set[str] = set()
    async with aiomqtt.Client(hostname=host, port=port, **mqtt_auth()) as client:
        await client.subscribe(f"vmc/{machine_id}/#")
        await client.subscribe(f"homeassistant/+/{machine_id}_vending/+/config")
        try:
            async with asyncio.timeout(1.0):
                async for msg in client.messages:
                    if msg.retain:
                        topics.add(str(msg.topic))
        except TimeoutError:
            pass
        for topic in topics:
            await client.publish(topic, b"", retain=True)


async def _wait_until_answering(host: str, port: int, machine_id: str) -> None:
    """Ping until the board acks, so no test races the simulator's startup."""
    async with BoardClient(host, port, machine_id) as client:
        for _ in range(15):
            rid = await client.send("ping")
            try:
                await client.acks(rid, 1, 2)
                return
            except TimeoutError:
                continue
    raise RuntimeError(f"board {machine_id} did not answer ping")


@pytest.fixture(scope="package")
def target():
    host, port = broker_address()
    if not _is_sim():
        machine_id = os.environ["ICE_COLDER_BOARD_MACHINE_ID"]
        asyncio.run(_wait_until_answering(host, port, machine_id))
        yield Target(host, port, machine_id, False)
        return

    machine_id = f"conf-{uuid.uuid4().hex[:8]}"
    sim = VendingMachineSimulator(
        broker=host, port=port, machine_id=machine_id, **mqtt_auth()
    )
    ready = threading.Event()
    state: dict = {}

    def run() -> None:
        async def main() -> None:
            state["task"] = asyncio.current_task()
            state["loop"] = asyncio.get_running_loop()
            ready.set()
            with contextlib.suppress(asyncio.CancelledError):
                await sim.run()

        asyncio.run(main())

    thread = threading.Thread(target=run, name="conformance-sim", daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        asyncio.run(_wait_until_answering(host, port, machine_id))
        yield Target(host, port, machine_id, True, sim.client_id)
    finally:
        state["loop"].call_soon_threadsafe(state["task"].cancel)
        thread.join(10)
        asyncio.run(_clear_retained(host, port, machine_id))


@pytest.fixture(scope="package")
def machine_id(target) -> str:
    return target.machine_id


@pytest.fixture(scope="package")
def is_sim(target) -> bool:
    return target.is_sim


@pytest.fixture
async def board(target):
    async with BoardClient(target.host, target.port, target.machine_id) as client:
        yield client
