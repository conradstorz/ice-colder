"""MDB board conformance fixtures.

Targets (``ICE_COLDER_BOARD_BROKER`` = ``host[:port]``, default ``localhost:1883``):
- ``ICE_COLDER_BOARD_MACHINE_ID`` set: hardware target on that broker/machine.
- unset: the in-process MDB simulator runs on a thread (own event loop,
  package-long) under a fresh ``machine_id``. Its random customer is replaced
  by one that never acts, so the tests are deterministic.

The opt-in skip (``ICE_COLDER_BOARD_TESTS`` unset) is the parent conftest's.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid

import pytest

from ice_sims.mdb.simulator import MdbSimulator
from tests.firmware.board_client import BoardClient, broker_address, mqtt_auth
from tests.firmware.conftest import Target, clear_retained


class _NoCustomer:
    """Stands in for `MdbCustomer`: nobody ever inserts cash."""

    async def run(self, _port) -> None:
        await asyncio.Event().wait()


async def _wait_until_answering(host: str, port: int, machine_id: str) -> None:
    async with BoardClient(host, port, machine_id, subsystem="mdb") as client:
        for _ in range(15):
            rid = await client.send("ping")
            try:
                await client.acks(rid, 1, 2)
                return
            except TimeoutError:
                continue
    raise RuntimeError(f"mdb board {machine_id} did not answer ping")


@pytest.fixture(scope="package")
def mdb_target():
    host, port = broker_address()
    machine_id = os.environ.get("ICE_COLDER_BOARD_MACHINE_ID")
    if machine_id:
        asyncio.run(_wait_until_answering(host, port, machine_id))
        yield Target(host, port, machine_id, False)
        return

    machine_id = f"conf-{uuid.uuid4().hex[:8]}"
    sim = MdbSimulator(
        broker=host,
        port=port,
        machine_id=machine_id,
        customer=_NoCustomer(),
        **mqtt_auth(),
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

    thread = threading.Thread(target=run, name="conformance-mdb-sim", daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        asyncio.run(_wait_until_answering(host, port, machine_id))
        yield Target(host, port, machine_id, True, sim.client_id)
    finally:
        state["loop"].call_soon_threadsafe(state["task"].cancel)
        thread.join(10)
        asyncio.run(clear_retained(host, port, machine_id))


@pytest.fixture
async def board(mdb_target):
    async with BoardClient(
        mdb_target.host, mdb_target.port, mdb_target.machine_id, subsystem="mdb"
    ) as client:
        yield client
