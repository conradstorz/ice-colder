"""Behaviour every concrete simulator must share (spec: standard commands).

Parametrized over each concrete ``ESP32Simulator`` that ``ice_sims`` exposes:
the ice maker monitor, the vending machine and the MDB gateway. Subclass-
specific behaviour stays in the per-simulator test modules.
"""

import json
from unittest.mock import AsyncMock

import pytest

from ice_contracts.board.common import SubsystemCommand
from ice_sims.ice_maker import IceMakerSimulator
from ice_sims.mdb.simulator import MdbSimulator
from ice_sims.vending_machine import VendingMachineSimulator

SIMULATORS = [
    pytest.param(lambda: IceMakerSimulator(machine_id="vmc-t"), id="ice_maker"),
    pytest.param(lambda: VendingMachineSimulator(machine_id="vmc-t"), id="vending"),
    pytest.param(lambda: MdbSimulator(machine_id="vmc-t"), id="mdb"),
]


@pytest.fixture(params=SIMULATORS)
def sim(request):
    return request.param()


@pytest.fixture
def client():
    return AsyncMock()


def _cmd(request_id: str, command: str) -> SubsystemCommand:
    return SubsystemCommand(request_id=request_id, command=command, params={})


def _published(client) -> list[tuple[str, dict]]:
    return [
        (call.args[0], json.loads(call.args[1]))
        for call in client.publish.call_args_list
    ]


def _acks(sim, client) -> list[dict]:
    topic = f"{sim.topic_prefix}/cmd/{sim.subsystem_name}/ack"
    return [payload for t, payload in _published(client) if t == topic]


async def test_ping_acks_ok_and_publishes_nothing_else(sim, client):
    """A real ping is a round trip only: no snapshot, no heartbeat."""
    await sim.handle_command(client, _cmd("req-ping0001", "ping"))
    published = _published(client)
    assert [t for t, _ in published] == [
        f"{sim.topic_prefix}/cmd/{sim.subsystem_name}/ack"
    ]
    ack = published[0][1]
    assert ack["status"] == "ok"
    assert ack["result"] is None


async def test_force_report_republishes_heartbeat_then_acks_ok(sim, client):
    await sim.handle_command(client, _cmd("req-force001", "force_report"))
    topics = [t for t, _ in _published(client)]
    heartbeat = f"{sim.topic_prefix}/heartbeat/{sim.subsystem_name}"
    assert topics.count(heartbeat) == 1
    acks = _acks(sim, client)
    assert len(acks) == 1 and acks[0]["status"] == "ok"
    assert topics[-1].endswith("/ack")


async def test_duplicate_request_id_runs_side_effect_once_and_replays_ack(sim, client):
    calls: list[str] = []

    async def bump(_client, cmd):
        calls.append(cmd.request_id)
        return {"token": "first-and-only"}

    sim.register_command("bump", bump)
    cmd = _cmd("req-dup00001", "bump")
    await sim.handle_command(client, cmd)
    await sim.handle_command(client, cmd)
    await sim.handle_command(client, cmd)

    assert calls == ["req-dup00001"]
    acks = _acks(sim, client)
    assert len(acks) == 3
    assert acks[0] == acks[1] == acks[2]
    assert acks[0]["result"] == {"token": "first-and-only"}
