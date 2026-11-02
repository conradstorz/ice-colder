"""A cash sale with change, end to end: the real MDB simulator and the real edge
(Machine, CommandDispatcher, MQTT client) over a real broker.

The customer is driven through the simulator's public methods (never the random
`MdbCustomer` loop), and the vend completes through the vending path the other
integration tests use. Skipped, like them, when no broker is on localhost:1883.
"""

import asyncio
import contextlib
import json
import os
import sys

import aiomqtt
import pytest
from pydantic import SecretStr

from ice_common.config_model import ConfigModel
from ice_common.mqtt_client import MQTTClient
from ice_contracts.board.common import CommandAck
from ice_contracts.board.mqtt_messages import ButtonPress
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from ice_edge.faults.availability import Availability
from ice_edge.hal.command_dispatcher import CommandDispatcher
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_sims.mdb.simulator import MdbSimulator
from tests.dispenser_fixtures import profiles_for

# Windows needs SelectorEventLoop for aiomqtt
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

AUTH = {
    "username": os.environ.get("MQTT_USERNAME") or None,
    "password": os.environ.get("MQTT_PASSWORD") or None,
}


async def _broker_reachable() -> bool:
    try:
        async with aiomqtt.Client(hostname="localhost", port=1883, **AUTH) as client:
            await client.publish("test/ping", "ok")
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not asyncio.run(_broker_reachable()),
        reason="MQTT broker not available on localhost:1883",
    ),
]


async def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("condition not met within timeout")


async def stop(task: "asyncio.Task") -> None:
    """Cancel a background task without hiding a crash it already had."""
    if task.done() and not task.cancelled() and task.exception() is not None:
        raise task.exception()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class _IdleCustomer:
    """Stands in for the random customer loop: does nothing, forever."""

    async def run(self, port) -> None:
        await asyncio.Event().wait()


def _config() -> ConfigModel:
    config = ConfigModel(
        machine_id="test-mdb-e2e",
        physical={
            "common_name": "MDB E2E",
            "serial_number": "test-mdb-e2e",
            "products": [
                {
                    "sku": "ICE-SM",
                    "name": "Small Ice",
                    "price_cents": 200,
                    "kind": "ice",
                    "slot": 0,
                    "button": "button_ice",
                }
            ],
        },
        mqtt={
            "broker_host": "localhost",
            "broker_port": 1883,
            "client_id": "e2e-mdb-vmc",
            "reconnect_interval": 1.0,
        },
    )
    if AUTH["username"]:
        config.mqtt.username = AUTH["username"]
    if AUTH["password"]:
        config.mqtt.password = SecretStr(AUTH["password"])
    return config


class _Collector:
    """Every message on the machine's `cmd/*` topics, decoded."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.messages: list[tuple[str, dict]] = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        async for msg in self._stream:
            try:
                payload = json.loads(msg.payload)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                self.messages.append((str(msg.topic), payload))

    def commands(self, suffix: str, name: str) -> list[dict]:
        return [
            p
            for t, p in self.messages
            if t.endswith(suffix) and p.get("command") == name
        ]

    async def next_dispense(self, timeout: float = 15.0) -> dict:
        await wait_until(lambda: self.commands("/cmd/vending", "dispense"), timeout)
        return self.commands("/cmd/vending", "dispense")[0]

    async def close(self) -> None:
        await stop(self._task)


@contextlib.asynccontextmanager
async def _edge(tmp_path, ack_timeout: float | None = None):
    """The real edge (Machine, CommandDispatcher, MQTT client) on the broker."""
    config = _config()
    prefix = f"vmc/{config.machine_id}"
    mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
    machine = Machine(config=config)
    health = HealthMonitor()
    availability = Availability()
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_mqtt_client(mqtt_client)
    machine.set_health_monitor(health)
    machine.set_availability(availability)
    machine.set_dispenser_profiles(profiles_for(config.physical.products, tmp_path))
    machine.set_command_dispatcher(
        CommandDispatcher(
            mqtt_client, **({} if ack_timeout is None else {"timeout": ack_timeout})
        )
    )
    # As main.py chains it: liveness and connection state reach the permissive
    # table, and the policy sync starts, through this callback.
    mqtt_client.set_connection_callback(
        lambda connected: (
            health.update_mqtt_status(connected),
            machine.on_mqtt_connection(connected),
        )
    )
    async with aiomqtt.Client(
        hostname="localhost", port=1883, identifier="e2e-mdb-probe", **AUTH
    ) as probe:
        await probe.subscribe(f"{prefix}/cmd/#")
        collector = _Collector(probe.messages)
        mqtt_task = asyncio.create_task(mqtt_client.run())
        try:
            await wait_until(lambda: mqtt_client.subscribed)
            yield machine, prefix, probe, collector
        finally:
            try:
                await stop(mqtt_task)
            finally:
                await collector.close()
                machine.cancel_pending_tasks()


async def test_cash_sale_with_change_settles_the_board(tmp_path):
    async with _edge(tmp_path) as (machine, prefix, probe, collector):
        vmc = machine.vmc
        sim = MdbSimulator(
            machine_id="test-mdb-e2e",
            customer=_IdleCustomer(),
            username=AUTH["username"],
            password=AUTH["password"],
        )
        sim.PAYOUT_DELAY_RANGE = (0.0, 0.0)
        sim_task = asyncio.create_task(sim.run())
        try:
            async with aiomqtt.Client(
                hostname="localhost", port=1883, identifier="e2e-mdb-hands", **AUTH
            ) as hands:
                # The other boards, alive and with ice in the bin.
                for name in ("vending", "ice_maker"):
                    await probe.publish(
                        f"{prefix}/heartbeat/{name}",
                        json.dumps({"subsystem": name, "uptime_seconds": 1}),
                    )
                await probe.publish(
                    f"{prefix}/hardware/io/bin_half_full",
                    json.dumps({"device": "bin_half_full", "state": True}),
                )

                # 1. The policy is acked and the board accepts.
                await wait_until(
                    lambda: sim.policy.acked_in_boot and sim.policy.policy.accept,
                    timeout=20.0,
                )

                # 2. A selection.
                await probe.publish(
                    f"{prefix}/hardware/buttons",
                    ButtonPress(button="button_ice").model_dump_json(),
                )
                await wait_until(lambda: vmc.state == "interacting_with_user")
                assert vmc.selected_product.price_cents == 200

                # 3. Three dollar coins, overpaying by 100.
                for _ in range(3):
                    assert await sim.insert_coin(hands, 100)
                await wait_until(lambda: vmc.state == "dispensing", timeout=15.0)
                assert sim.cash.escrow_cents == 300

                # 4. The vend, completed through the vending path.
                dispense = await collector.next_dispense()
                request_id = dispense["request_id"]
                await probe.publish(
                    f"{prefix}/cmd/vending/ack",
                    CommandAck(
                        request_id=request_id,
                        command="dispense",
                        status="ok",
                        phase="accepted",
                    ).model_dump_json(),
                )
                # Deliberate fixed sleep: the dispatcher exposes no public "ack
                # consumed" observable and the FSM stays in "dispensing"
                # meanwhile; the terminal report must follow the ack.
                await asyncio.sleep(0.3)
                await probe.publish(
                    f"{prefix}/hardware/dispenser",
                    json.dumps(
                        {"slot": 0, "state": "complete", "request_id": request_id}
                    ),
                )

                # 5-7. Change paid, cash_sale sent, board escrow back to 0.
                await wait_until(lambda: vmc.state == "idle", timeout=15.0)
                await wait_until(lambda: sim.cash.escrow_cents == 0, timeout=15.0)
                payouts = collector.commands("/cmd/mdb", "payout")
                sales = collector.commands("/cmd/mdb", "cash_sale")
                assert [p["params"]["cents"] for p in payouts] == [100]
                assert [p["params"]["cents"] for p in sales] == [200]
                assert vmc.credit_escrow_cents == 0
                assert len(collector.commands("/cmd/vending", "dispense")) == 1

                # 8. No payment faults.
                active = machine.faults.machine_faults
                assert not [c for c in active if str(c).startswith("PAY-10")], active
        finally:
            await stop(sim_task)


class _LossySim(MdbSimulator):
    """Loses a payout's ack and result on the wire (`lose_payout`) and can go
    silent about a reboot (`mute`): the window before the edge sees the board's
    new boot."""

    lose_payout = False
    mute = False

    async def publish(self, client, topic_suffix, payload, retain=False, qos=1):
        if self.lose_payout and topic_suffix == "cmd/mdb/ack":
            if getattr(payload, "command", None) == "payout":
                return
        if self.lose_payout and topic_suffix == "payment/event":
            if (
                getattr(payload, "event", None)
                and payload.event.kind == "payout_result"
            ):
                return
        await super().publish(client, topic_suffix, payload, retain=retain, qos=qos)

    async def publish_state(self, client):
        if not self.mute:
            await super().publish_state(client)

    async def replay(self, client):
        if not self.mute:
            await super().replay(client)


async def test_payout_executed_ack_lost_board_reboots_pays_exactly_once(tmp_path):
    async with _edge(tmp_path, ack_timeout=1.0) as (machine, prefix, probe, collector):
        vmc = machine.vmc
        sim = _LossySim(
            machine_id="test-mdb-e2e",
            customer=_IdleCustomer(),
            username=AUTH["username"],
            password=AUTH["password"],
        )
        sim.PAYOUT_DELAY_RANGE = (0.0, 0.0)
        sim_task = asyncio.create_task(sim.run())
        try:
            async with aiomqtt.Client(
                hostname="localhost", port=1883, identifier="e2e-mdb-hands", **AUTH
            ) as hands:
                for name in ("vending", "ice_maker"):
                    await probe.publish(
                        f"{prefix}/heartbeat/{name}",
                        json.dumps({"subsystem": name, "uptime_seconds": 1}),
                    )
                await wait_until(
                    lambda: sim.policy.acked_in_boot and sim.policy.policy.accept,
                    timeout=20.0,
                )
                for _ in range(3):
                    assert await sim.insert_coin(hands, 100)
                await wait_until(lambda: vmc.credit_escrow_cents == 300)
                tubes_before = sim.cash.tube_total_cents()

                # The payout executes on the board but its ack and result are lost.
                sim.lose_payout = True
                vmc.request_refund(reason="customer")
                await wait_until(
                    lambda: sim.cash.tube_total_cents() == tubes_before - 300
                )
                await wait_until(
                    lambda: any(
                        p["maybe_delivered"] for p in vmc.snapshot().pending_payouts
                    ),
                    timeout=15.0,
                )

                # The board reboots; the edge has not yet seen its new boot.
                sim.lose_payout = False
                sim.mute = True
                await sim.reboot(hands)
                # The edge re-sends under the same id into a policy-less board.
                await wait_until(
                    lambda: str(FaultCode.PAY_103)
                    in {str(c) for c in machine.faults.machine_faults},
                    timeout=20.0,
                )
                assert vmc.snapshot().pending_payouts == []
                sim.mute = False
                await sim.publish_state(hands)
                await sim.replay(hands)
                await wait_until(lambda: sim.policy.acked_in_boot, timeout=20.0)

                ids = {
                    p["request_id"] for p in collector.commands("/cmd/mdb", "payout")
                }
                assert len(ids) == 1  # never re-minted
                assert sim.cash.tube_total_cents() == tubes_before - 300  # paid once
        finally:
            await stop(sim_task)


async def test_board_reboot_while_edge_holds_cash_sells_without_pay104(tmp_path):
    async with _edge(tmp_path) as (machine, prefix, probe, collector):
        vmc = machine.vmc
        sim = MdbSimulator(
            machine_id="test-mdb-e2e",
            customer=_IdleCustomer(),
            username=AUTH["username"],
            password=AUTH["password"],
        )
        sim.PAYOUT_DELAY_RANGE = (0.0, 0.0)
        sim_task = asyncio.create_task(sim.run())
        try:
            async with aiomqtt.Client(
                hostname="localhost", port=1883, identifier="e2e-mdb-hands", **AUTH
            ) as hands:
                for name in ("vending", "ice_maker"):
                    await probe.publish(
                        f"{prefix}/heartbeat/{name}",
                        json.dumps({"subsystem": name, "uptime_seconds": 1}),
                    )
                await probe.publish(
                    f"{prefix}/hardware/io/bin_half_full",
                    json.dumps({"device": "bin_half_full", "state": True}),
                )
                await wait_until(
                    lambda: sim.policy.acked_in_boot and sim.policy.policy.accept,
                    timeout=20.0,
                )
                # 100 cents on the first boot ...
                assert await sim.insert_coin(hands, 100)
                await wait_until(lambda: vmc.credit_escrow_cents == 100)
                # ... then the board reboots: its escrow is gone, the edge's is not.
                await sim.reboot(hands)
                assert sim.cash.escrow_cents == 0
                await wait_until(
                    lambda: sim.policy.acked_in_boot and sim.policy.policy.accept,
                    timeout=20.0,
                )
                for _ in range(2):
                    assert await sim.insert_coin(hands, 100)
                await wait_until(lambda: vmc.credit_escrow_cents == 300)
                await probe.publish(
                    f"{prefix}/hardware/buttons",
                    ButtonPress(button="button_ice").model_dump_json(),
                )
                await wait_until(lambda: vmc.state == "dispensing", timeout=15.0)
                dispense = await collector.next_dispense()
                request_id = dispense["request_id"]
                await probe.publish(
                    f"{prefix}/cmd/vending/ack",
                    CommandAck(
                        request_id=request_id,
                        command="dispense",
                        status="ok",
                        phase="accepted",
                    ).model_dump_json(),
                )
                # Deliberate fixed sleep, as in the other e2e test.
                await asyncio.sleep(0.3)
                await probe.publish(
                    f"{prefix}/hardware/dispenser",
                    json.dumps(
                        {"slot": 0, "state": "complete", "request_id": request_id}
                    ),
                )
                await wait_until(lambda: vmc.state == "idle", timeout=15.0)
                await wait_until(
                    lambda: not vmc.snapshot().pending_payouts, timeout=15.0
                )
                await wait_until(lambda: sim.cash.escrow_cents == 0, timeout=15.0)
                payouts = collector.commands("/cmd/mdb", "payout")
                sales = collector.commands("/cmd/mdb", "cash_sale")
                # Price 200 = 100 (boot A) + 100 (boot B): only the boot-B part
                # left the board's escrow; the 100 change is paid from the tubes.
                assert [p["params"]["cents"] for p in payouts] == [100]
                assert [p["params"]["cents"] for p in sales] == [100]
                active = machine.faults.machine_faults
                assert not [c for c in active if str(c).startswith("PAY-10")], active
        finally:
            await stop(sim_task)
