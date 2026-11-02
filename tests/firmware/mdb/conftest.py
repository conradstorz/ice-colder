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
import os
import uuid

import pytest

from ice_sims.mdb.simulator import MdbSimulator
from tests.firmware.board_client import BoardClient, broker_address, mqtt_auth
from tests.firmware.conftest import (
    Target,
    clear_retained,
    sim_on_thread,
    wait_until_answering,
)


class _NoCustomer:
    """Stands in for `MdbCustomer`: nobody ever inserts cash."""

    async def run(self, _port) -> None:
        await asyncio.Event().wait()


@pytest.fixture(scope="package")
def mdb_target():
    host, port = broker_address()
    machine_id = os.environ.get("ICE_COLDER_BOARD_MACHINE_ID")
    if machine_id:
        asyncio.run(wait_until_answering(host, port, machine_id, "mdb"))
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
    try:
        with sim_on_thread(sim, "conformance-mdb-sim"):
            asyncio.run(wait_until_answering(host, port, machine_id, "mdb"))
            yield Target(host, port, machine_id, True, sim.client_id)
    finally:
        asyncio.run(clear_retained(host, port, machine_id))


@pytest.fixture
async def board(mdb_target):
    async with BoardClient(
        mdb_target.host, mdb_target.port, mdb_target.machine_id, subsystem="mdb"
    ) as client:
        yield client
