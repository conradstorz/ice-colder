"""Entry-module and wire-surface tests for the MDB simulator.

Behaviour is covered in tests/sims/mdb/; this file pins what the compose
files, the fleet launcher and the CI smoke step depend on.
"""

import asyncio
import contextlib
from pathlib import Path

import ice_sims
from ice_sims import mdb_gateway
from ice_sims.base import ESP32Simulator
from ice_sims.mdb.simulator import MdbSimulator
from tests.async_helpers import settle
from tests.sims.mdb.fakes import PREFIX, FakeClient, policy, send

LEGACY_TOPICS = (
    "payment/credit",
    "payment/status",
    "cmd/payment/enable",
    "cmd/payment/refund",
)


def test_entry_module_exposes_the_runtime_simulator():
    assert mdb_gateway.MdbSimulator is MdbSimulator
    assert issubclass(MdbSimulator, ESP32Simulator)


def test_subsystem_name_and_connected_line_are_unchanged():
    assert MdbSimulator(machine_id="vmc-t").subsystem_name == "mdb"
    base_source = Path(ESP32Simulator.run.__code__.co_filename).read_text(
        encoding="utf-8"
    )
    # CI's smoke step greps the "[mdb] Connected" line this f-string produces.
    assert 'f"[{self.subsystem_name}] Connected to {self.broker}:{self.port}"' in (
        base_source
    )


def test_no_legacy_payment_topic_remains_in_the_simulators():
    root = Path(ice_sims.__file__).parent
    offenders = [
        (path.name, topic)
        for path in root.rglob("*.py")
        for topic in LEGACY_TOPICS
        if topic in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


async def test_it_subscribes_only_to_contract_topics():
    sim = MdbSimulator(machine_id="vmc-t")
    client = FakeClient()
    task = asyncio.create_task(sim.run_simulation(client))
    await settle(10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert sorted(client.subscribed) == sorted(
        [
            f"{PREFIX}/payment/event/ack",
            f"{PREFIX}/status",
            f"{PREFIX}/cmd/mdb",
        ]
    )


def test_prices_come_from_the_config_in_cents():
    sim = MdbSimulator(machine_id="vmc-t")
    for product in sim.config.products:
        assert sim.product_prices[product.name] == product.price_cents


async def test_legacy_commands_are_unsupported():
    sim = MdbSimulator(machine_id="vmc-t")
    client = FakeClient()
    await send(sim, client, "set_policy", policy())
    for legacy in ("payment/enable", "refund", "bill_acceptor_test"):
        assert (await send(sim, client, legacy)).status == "unsupported"
