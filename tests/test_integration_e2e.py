# tests/test_integration_e2e.py
"""
End-to-end integration tests using a real MQTT broker (mosquitto).

These tests exercise the full transaction loop:
  button press → product selection → payment → dispense command → complete → idle

Requirements:
  - mosquitto running on localhost:1883 (docker compose -f docker/docker-compose.yml up -d)

Tests are skipped automatically if the broker is unreachable (locally only: CI
starts a broker and its skip guard fails the build if these skip).
"""

import asyncio
import contextlib
import json
import os
import sys

import pytest
from pydantic import SecretStr

from ice_common.config_model import ConfigModel, Product
from ice_contracts.board.common import CommandAck
from controller.machine import Machine
from controller.vmc import VMC
from tests.fakes import FakeTaskRunner
from ice_edge.hal.command_dispatcher import CommandDispatcher
from services.health_monitor import HealthMonitor
from ice_common.mqtt_client import MQTTClient
from ice_contracts.board.mqtt_messages import (
    ButtonPress,
    DispenserStatus,
    PaymentEvent,
    SensorReading,
)
from tests.dispenser_fixtures import profiles_for

# Windows needs SelectorEventLoop for aiomqtt
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiomqtt

    _BROKER_AVAILABLE = None  # checked at module scope below
except ImportError:
    _BROKER_AVAILABLE = False

_MQTT_AUTH = {
    "username": os.environ.get("MQTT_USERNAME") or None,
    "password": os.environ.get("MQTT_PASSWORD") or None,
}


async def _check_broker():
    """Return True if mosquitto is reachable on localhost:1883."""
    try:
        async with aiomqtt.Client(
            hostname="localhost", port=1883, **_MQTT_AUTH
        ) as client:
            await client.publish("test/ping", "ok")
        return True
    except Exception:
        return False


if _BROKER_AVAILABLE is None:
    _BROKER_AVAILABLE = asyncio.run(_check_broker())

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _BROKER_AVAILABLE, reason="MQTT broker not available on localhost:1883"
    ),
]


def _make_config() -> ConfigModel:
    """Create a minimal config for testing with known products.

    `kind`/`slot` are set (and match each product's button index) so a
    dispense actually reaches the vending simulator through the real
    `DispenseCommand`/`DispenserProfiles` path (review finding I2) rather
    than taking the `CFG-101` "no valid profile" branch -- `select_product`
    refuses a `kind="other"` product the moment a `DispenserProfiles` is
    wired, and `on_dispense_product` would otherwise never dispatch
    anything onto `cmd/vending` at all.
    """
    config = ConfigModel(
        machine_id="test-e2e",
        physical={
            "common_name": "E2E Test Machine",
            "serial_number": "test-e2e",
            "products": [
                {
                    "sku": "ICE-SM",
                    "name": "Small Ice",
                    "price": 2.00,
                    "kind": "ice",
                    "slot": 0,
                },
                {
                    "sku": "ICE-LG",
                    "name": "Large Ice",
                    "price": 3.50,
                    "kind": "ice",
                    "slot": 1,
                },
                {
                    "sku": "WATER",
                    "name": "Purified Water",
                    "price": 1.50,
                    "kind": "water",
                    "slot": 2,
                },
            ],
        },
        mqtt={
            "broker_host": "localhost",
            "broker_port": 1883,
            "client_id": "e2e-test-vmc",
            "reconnect_interval": 1.0,
        },
    )
    if _MQTT_AUTH["username"]:
        config.mqtt.username = _MQTT_AUTH["username"]
    if _MQTT_AUTH["password"]:
        config.mqtt.password = SecretStr(_MQTT_AUTH["password"])
    return config


async def _wait_until(predicate, timeout: float = 10.0):
    """Poll a predicate until it is true or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("condition not met within timeout")


async def _stop_background_task(task: "asyncio.Task") -> None:
    """Tear down a background task (e.g. ``mqtt_client.run()``) without
    hiding a crash.

    If the task already finished with an exception, re-raise it so the real
    failure shows instead of an unrelated state-wait timeout. Otherwise
    cancel it and swallow only the resulting ``CancelledError``.
    """
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _wait_for_state(vmc: VMC, target_state: str, timeout: float = 10.0):
    """Poll VMC state until it reaches target_state or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if vmc.state == target_state:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"VMC did not reach state '{target_state}' within {timeout}s (current: {vmc.state})"
    )


def _wire_dispenser_runtime(
    machine: Machine, mqtt_client: MQTTClient, config: ConfigModel, tmp_path
) -> CommandDispatcher:
    """Attach a loaded `DispenserProfiles` and a real `CommandDispatcher` --
    constructed exactly as `main.py` does (`CommandDispatcher(mqtt_client)`,
    registering its `cmd/+/ack` handler before `mqtt_client.run()` ever
    subscribes) -- so a production dispense actually reaches `cmd/vending`
    instead of taking the `CFG-101` "no valid profile" branch (review
    finding I2). Must be called before the caller creates the
    `mqtt_client.run()` task."""
    profiles = profiles_for(config.physical.products, tmp_path)
    machine.set_dispenser_profiles(profiles)
    dispatcher = CommandDispatcher(mqtt_client)
    machine.set_command_dispatcher(dispatcher)
    return dispatcher


async def _ack_dispense(sim_client: "aiomqtt.Client", prefix: str, request_id: str):
    """Publish the "accepted" `CommandAck` a real vending board sends back
    on `cmd/vending/ack`, echoing *request_id* -- unblocking the VMC's
    `CommandDispatcher.send()` call, which is what actually sends the
    dispense command and is awaited before the dispense timer starts.
    Callers must publish this before the terminal `hardware/dispenser`
    report, matching the real accept-then-complete sequence."""
    await sim_client.publish(
        f"{prefix}/cmd/vending/ack",
        CommandAck(
            request_id=request_id,
            command="dispense",
            status="ok",
            phase="accepted",
        ).model_dump_json(),
    )
    # Give the dispatcher's cmd/+/ack handler and DispenseCycle._run a
    # moment to actually process the ack before the terminal report (sent
    # by the caller right after this returns) arrives.
    #
    # Deliberate residual fixed sleep: the dispatcher exposes no public
    # "ack consumed" observable (its pending-ack bookkeeping is private),
    # and on the success path the dispense cycle produces no state change
    # to wait on instead -- the FSM stays in "dispensing" throughout.
    await asyncio.sleep(0.3)


class _MessageCollector:
    """Drain an aiomqtt message stream into a list for the test's lifetime.

    The single ``messages.__anext__()`` the tests used to do could only ever
    see the first message, so a second ``dispense`` publish (the double-
    dispense money bug the dispatcher's same-request_id retry is designed
    around) went unnoticed. Collecting everything lets a test assert exactly
    one ``dispense`` command per sale.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._task: "asyncio.Task | None" = None
        self._cursor = 0
        # (topic, decoded JSON payload or None when not JSON)
        self.messages: list[tuple[str, object]] = []

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        if self._task is not None:
            await _stop_background_task(self._task)

    async def _pump(self) -> None:
        async for msg in self._stream:
            try:
                payload = json.loads(msg.payload)
            except (TypeError, ValueError):
                payload = None
            self.messages.append((str(msg.topic), payload))

    @property
    def dispenses(self) -> list[dict]:
        return [
            payload
            for topic, payload in self.messages
            if topic.endswith("/cmd/vending")
            and isinstance(payload, dict)
            and payload.get("command") == "dispense"
        ]

    async def wait_for(self, predicate, what: str, timeout: float = 5.0):
        """Return the first collected message payload matching *predicate*."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._task is not None and self._task.done():
                _ = self._task.result()  # re-raise a pump crash
            for topic, payload in self.messages:
                if predicate(topic, payload):
                    return payload
            if asyncio.get_running_loop().time() >= deadline:
                seen = [t for t, _ in self.messages]
                raise AssertionError(
                    f"{what} not received within {timeout}s (saw topics: {seen})"
                )
            await asyncio.sleep(0.05)

    async def next_dispense(self, timeout: float = 5.0) -> dict:
        """Return the next not-yet-consumed ``dispense`` command."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._task is not None and self._task.done():
                _ = self._task.result()  # re-raise a pump crash
            dispenses = self.dispenses
            if len(dispenses) > self._cursor:
                self._cursor += 1
                return dispenses[self._cursor - 1]
            if asyncio.get_running_loop().time() >= deadline:
                seen = [t for t, _ in self.messages]
                raise AssertionError(
                    f"No dispense command received within {timeout}s "
                    f"(saw topics: {seen})"
                )
            await asyncio.sleep(0.05)

    def assert_one_dispense_per_sale(self, request_ids: list[str]) -> None:
        """Exactly one ``dispense`` per sale: one per expected request_id and
        no extras (a same-request_id retry or a second sale's command)."""
        got = [d["request_id"] for d in self.dispenses]
        assert got == request_ids, (
            f"expected exactly one dispense per sale {request_ids}, got {got}"
        )


class _E2E:
    """Everything one e2e test needs, built by :func:`_e2e_harness`."""

    def __init__(self, config, prefix, machine, health, sim_client, collector):
        self.config = config
        self.prefix = prefix
        self.machine = machine
        self.vmc = machine.vmc
        self.health = health
        self.sim_client = sim_client
        self.collector = collector


@contextlib.asynccontextmanager
async def _e2e_harness(
    sim_id: str,
    tmp_path=None,
    *,
    config: ConfigModel | None = None,
    subscribe: tuple[str, ...] = ("cmd/vending",),
    with_health: bool = False,
    wire_dispenser: bool = True,
    tasks=None,
):
    """Shared e2e setup/teardown: MQTT client, Machine, optional
    HealthMonitor, a separate simulator client (*sim_id*) subscribed to
    *subscribe* (suffixes under the machine prefix) with its messages
    collected, the dispenser runtime (needs *tmp_path*), the connect wait,
    and teardown of the background MQTT task."""
    config = config or _make_config()
    prefix = f"vmc/{config.machine_id}"
    mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
    machine = Machine(config=config, tasks=tasks) if tasks else Machine(config=config)
    health = HealthMonitor() if with_health else None

    async with aiomqtt.Client(
        hostname="localhost", port=1883, identifier=sim_id, **_MQTT_AUTH
    ) as sim_client:
        for suffix in subscribe:
            await sim_client.subscribe(f"{prefix}/{suffix}")
        machine.attach_to_loop(asyncio.get_running_loop())
        machine.set_mqtt_client(mqtt_client)
        if health is not None:
            machine.set_health_monitor(health)
        if wire_dispenser:
            # Review finding I2: without a loaded DispenserProfiles and a
            # real CommandDispatcher, on_dispense_product takes the
            # CFG-101 "no valid profile" branch and never publishes
            # anything on cmd/vending at all.
            _wire_dispenser_runtime(machine, mqtt_client, config, tmp_path)

        collector = _MessageCollector(sim_client.messages)
        collector.start()
        mqtt_task = asyncio.create_task(mqtt_client.run())
        try:
            for _ in range(50):
                if mqtt_client.connected:
                    break
                await asyncio.sleep(0.1)
            assert mqtt_client.connected, "MQTT client failed to connect"
            yield _E2E(config, prefix, machine, health, sim_client, collector)
        finally:
            try:
                await _stop_background_task(mqtt_task)
            finally:
                await collector.stop()


class TestFullTransactionLoop:
    """Test the complete vend cycle through real MQTT."""

    async def test_button_payment_dispense_complete(self, tmp_path):
        """Full happy path: button → payment → dispense → complete → idle."""
        async with _e2e_harness("e2e-simulator", tmp_path, with_health=True) as h:
            vmc, prefix, sim_client = h.vmc, h.prefix, h.sim_client

            # 1. Simulate button press (button 0 = Small Ice, $2.00)
            assert vmc.state == "idle"
            await sim_client.publish(
                f"{prefix}/hardware/buttons",
                ButtonPress(button=0).model_dump_json(),
            )

            # VMC should transition to interacting_with_user
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)
            assert vmc.selected_product.name == "Small Ice"

            # 2. Simulate payment (exact amount)
            await sim_client.publish(
                f"{prefix}/payment/credit",
                PaymentEvent(amount=2.00, method="cash_bill").model_dump_json(),
            )

            # VMC should process payment → transition to dispensing
            await _wait_for_state(vmc, "dispensing", timeout=5.0)

            # 3. Verify dispense command was published. The payload is a
            # SubsystemCommand (command="dispense"), carrying the slot
            # inside its validated params.
            dispense_msg = await h.collector.next_dispense()
            assert dispense_msg["params"]["slot"] == 0
            assert dispense_msg["params"]["mechanism"] == "bagged_ice"

            # 4. Accept the command (the real board's first ack), then
            # report completion, both correlated by request_id.
            request_id = dispense_msg["request_id"]
            await _ack_dispense(sim_client, prefix, request_id)
            await sim_client.publish(
                f"{prefix}/hardware/dispenser",
                DispenserStatus(
                    slot=0, state="complete", request_id=request_id
                ).model_dump_json(),
            )

            # VMC should return to idle (no remaining credit)
            await _wait_for_state(vmc, "idle", timeout=5.0)
            assert vmc.credit_escrow == 0.0
            h.collector.assert_one_dispense_per_sale([request_id])

    async def test_overpayment_returns_to_idle_with_change(self, tmp_path):
        """Overpayment: VMC dispenses and remaining credit is consumed or idle."""
        async with _e2e_harness("e2e-sim-overpay", tmp_path) as h:
            vmc, prefix, sim_client = h.vmc, h.prefix, h.sim_client

            # Button press for Water ($1.50)
            await sim_client.publish(
                f"{prefix}/hardware/buttons",
                ButtonPress(button=2).model_dump_json(),
            )
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)

            # Overpay with $5.00
            await sim_client.publish(
                f"{prefix}/payment/credit",
                PaymentEvent(amount=5.00, method="cash_bill").model_dump_json(),
            )
            await _wait_for_state(vmc, "dispensing", timeout=5.0)

            # Credit should be $5.00 - $1.50 = $3.50
            assert abs(vmc.credit_escrow - 3.50) < 0.01

            dispense_msg = await h.collector.next_dispense()
            assert dispense_msg["params"]["slot"] == 2
            assert dispense_msg["params"]["mechanism"] == "water_fill"

            # Accept, then report completion, both correlated by
            # request_id.
            request_id = dispense_msg["request_id"]
            await _ack_dispense(sim_client, prefix, request_id)
            await sim_client.publish(
                f"{prefix}/hardware/dispenser",
                DispenserStatus(
                    slot=2, state="complete", request_id=request_id
                ).model_dump_json(),
            )

            # With remaining credit, VMC goes back to interacting_with_user
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)
            assert abs(vmc.credit_escrow - 3.50) < 0.01
            h.collector.assert_one_dispense_per_sale([request_id])

    async def test_insufficient_funds_waits_for_more(self, tmp_path):
        """Underpayment: VMC stays in interacting_with_user until enough is deposited."""
        async with _e2e_harness("e2e-sim-underpay", tmp_path) as h:
            vmc, prefix, sim_client = h.vmc, h.prefix, h.sim_client

            # Button press for Large Ice ($3.50)
            await sim_client.publish(
                f"{prefix}/hardware/buttons",
                ButtonPress(button=1).model_dump_json(),
            )
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)

            # Insert $1.00 — not enough
            await sim_client.publish(
                f"{prefix}/payment/credit",
                PaymentEvent(amount=1.00, method="cash_coin").model_dump_json(),
            )
            # Wait for the positive part -- escrow reflecting the
            # deposit -- then check the negative: state unchanged.
            # Credit never becomes sufficient ($1.00 < $3.50), so the
            # state assertion holds regardless of when process_payment's
            # own timer happens to re-check.
            await _wait_until(lambda: abs(vmc.credit_escrow - 1.00) < 0.01)
            assert vmc.state == "interacting_with_user"
            assert h.collector.dispenses == []

            # Insert another $3.00 — now $4.00 total >= $3.50
            await sim_client.publish(
                f"{prefix}/payment/credit",
                PaymentEvent(amount=3.00, method="cash_bill").model_dump_json(),
            )

            # Now it should transition to dispensing
            await _wait_for_state(vmc, "dispensing", timeout=10.0)
            # Credit should be $4.00 - $3.50 = $0.50
            assert abs(vmc.credit_escrow - 0.50) < 0.01

            dispense_msg = await h.collector.next_dispense()
            request_id = dispense_msg["request_id"]
            await _ack_dispense(sim_client, prefix, request_id)

            # Complete the dispense
            await sim_client.publish(
                f"{prefix}/hardware/dispenser",
                DispenserStatus(
                    slot=1, state="complete", request_id=request_id
                ).model_dump_json(),
            )
            # With $0.50 remaining, goes back to interacting
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)
            h.collector.assert_one_dispense_per_sale([request_id])

    async def test_dispenser_fault_fails_vend_and_locks_product(self, tmp_path):
        """Dispenser jam during vend runs vend_failed: escrow restored, product locked."""
        async with _e2e_harness("e2e-sim-error", tmp_path) as h:
            vmc, prefix, sim_client = h.vmc, h.prefix, h.sim_client

            # Button + payment (button 0 = Small Ice, ICE-SM, $2.00)
            await sim_client.publish(
                f"{prefix}/hardware/buttons",
                ButtonPress(button=0).model_dump_json(),
            )
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)

            await sim_client.publish(
                f"{prefix}/payment/credit",
                PaymentEvent(amount=2.00, method="card").model_dump_json(),
            )
            await _wait_for_state(vmc, "dispensing", timeout=5.0)

            dispense_msg = await h.collector.next_dispense()
            assert dispense_msg["params"]["slot"] == 0
            assert dispense_msg["params"]["mechanism"] == "bagged_ice"

            # Accept, then the dispenser reports a jam instead of
            # complete -- both correlated by request_id.
            request_id = dispense_msg["request_id"]
            await _ack_dispense(sim_client, prefix, request_id)
            await sim_client.publish(
                f"{prefix}/hardware/dispenser",
                DispenserStatus(
                    slot=0, state="jam", request_id=request_id
                ).model_dump_json(),
            )
            await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)
            assert vmc.credit_escrow == 2.00
            assert vmc.faults.lockouts["ICE-SM"].value == "ICE-401"
            assert vmc.state != "error"
            h.collector.assert_one_dispense_per_sale([request_id])


class TestSensorAndHeartbeatRouting:
    """Test that sensor readings and heartbeats flow through to HealthMonitor."""

    async def test_sensor_readings_reach_health_monitor(self):
        async with _e2e_harness(
            "e2e-sim-sensor", subscribe=(), with_health=True, wire_dispenser=False
        ) as h:
            # Publish a temperature reading
            await h.sim_client.publish(
                f"{h.prefix}/sensors/temp/compressor",
                SensorReading(location="compressor", value=65.3).model_dump_json(),
            )
            await _wait_until(
                lambda: "compressor" in h.health.get_summary()["temperatures"]
            )

            summary = h.health.get_summary()
            assert "compressor" in summary["temperatures"]
            assert abs(summary["temperatures"]["compressor"]["value"] - 65.3) < 0.1

    async def test_heartbeat_reaches_health_monitor(self):
        async with _e2e_harness(
            "e2e-sim-hb", subscribe=(), with_health=True, wire_dispenser=False
        ) as h:
            # Publish a heartbeat
            await h.sim_client.publish(
                f"{h.prefix}/heartbeat/ice_maker",
                json.dumps({"subsystem": "ice_maker", "uptime_seconds": 120}),
            )
            await _wait_until(
                lambda: "ice_maker" in h.health.get_summary()["subsystems"]
            )

            summary = h.health.get_summary()
            assert "ice_maker" in summary["subsystems"]


class TestFailedVendLoop:
    """jam → vend_failed → lockout → clear → sell again, through a real broker."""

    async def test_jam_locks_product_then_clear_sells_again(self, tmp_path):
        # A FakeTaskRunner still runs fire-and-forget coroutines (the MQTT
        # publishes) on the real loop; it only makes timers explicit, so
        # the customer walking away below is runner.fire("session_timeout")
        # rather than a 180 s wait or a call into a private handler.
        runner = FakeTaskRunner()
        async with _e2e_harness(
            "e2e-fault-sim",
            tmp_path,
            subscribe=("cmd/vending", "cmd/payment/refund"),
            with_health=True,
            tasks=runner,
        ) as h:
            vmc, prefix, sim_client = h.vmc, h.prefix, h.sim_client
            request_ids: list[str] = []

            async def sale(outcome: str):
                await sim_client.publish(
                    f"{prefix}/hardware/buttons", json.dumps({"button": 0})
                )
                await _wait_for_state(vmc, "interacting_with_user")
                await sim_client.publish(
                    f"{prefix}/payment/credit",
                    json.dumps({"amount": 2.00, "method": "cash_bill"}),
                )
                # select_product schedules process_payment a second
                # later; with the fake runner the test fires it once
                # the credit has landed in escrow.
                await _wait_until(lambda: vmc.credit_escrow >= 2.00)
                runner.fire("process_payment")
                await _wait_for_state(vmc, "dispensing")

                dispense_msg = await h.collector.next_dispense()
                request_id = dispense_msg["request_id"]
                request_ids.append(request_id)
                await _ack_dispense(sim_client, prefix, request_id)
                await sim_client.publish(
                    f"{prefix}/hardware/dispenser",
                    json.dumps({"slot": 0, "state": outcome, "request_id": request_id}),
                )

            try:
                await sale("jam")
                await _wait_for_state(vmc, "interacting_with_user")
                assert vmc.credit_escrow == 2.00
                assert vmc.faults.lockouts["ICE-SM"].value == "ICE-401"
                assert h.health.get_summary()["active_faults"][0]["code"] == "ICE-401"
                h.collector.assert_one_dispense_per_sale(request_ids)

                # Customer walks away: the session timeout _fail_vend
                # re-armed on the way back to interacting_with_user fires,
                # and session expiry pays out via the gateway.
                runner.fire("session_timeout")

                refund = await h.collector.wait_for(
                    lambda topic, payload: topic.endswith("/cmd/payment/refund"),
                    "refund command",
                )
                assert refund["amount"] == 2.00
                await _wait_for_state(vmc, "idle")

                assert vmc.clear_fault("ICE-SM") is True
                await sale("complete")
                await _wait_for_state(vmc, "idle")
                assert vmc.faults.lockouts == {}
                # One dispense per sale, each with its own request_id.
                assert len(request_ids) == 2
                assert request_ids[0] != request_ids[1]
                h.collector.assert_one_dispense_per_sale(request_ids)
            finally:
                h.machine.cancel_pending_tasks()


async def test_vending_heartbeat_loss_blocks_sale_not_payment_enable():
    """Live broker: vending LWT no longer withdraws payment/enable —
    `vending_alive` is a fulfillment-only permissive under the soft-faults
    design (services/availability.py), so cmd/payment/enable stays true
    throughout. Heartbeat loss instead blocks `sale_available` for that
    product kind, and heartbeat recovery restores it."""
    from services.availability import Availability

    cfg = ConfigModel()
    cfg.machine_id = "e2e-enable"
    cfg.physical.products = [Product(sku="ICE-1", name="Ice", price=1.0, kind="ice")]
    if _MQTT_AUTH["username"]:
        cfg.mqtt.username = _MQTT_AUTH["username"]
    if _MQTT_AUTH["password"]:
        cfg.mqtt.password = SecretStr(_MQTT_AUTH["password"])
    mqtt = MQTTClient(config=cfg.mqtt, machine_id=cfg.machine_id)
    machine = Machine(config=cfg)
    machine.attach_to_loop(asyncio.get_running_loop())
    monitor = HealthMonitor()
    machine.set_health_monitor(monitor)
    avail = Availability()
    machine.set_availability(avail)
    machine.set_mqtt_client(mqtt)
    mqtt.set_connection_callback(
        lambda c: (monitor.update_mqtt_status(c), machine.on_mqtt_connection(c))
    )
    run = asyncio.create_task(mqtt.run())

    seen: list[bool] = []
    async with aiomqtt.Client(hostname="localhost", port=1883, **_MQTT_AUTH) as probe:
        await probe.subscribe(f"vmc/{cfg.machine_id}/cmd/payment/enable")
        # `mqtt.connected` flips true before the subscribe loop runs, so it
        # does not guarantee the VMC is subscribed to the heartbeat/payment/
        # hardware-io topics published below. `mqtt.subscribed` is the
        # signal that does: it only goes true once every registered topic
        # has been subscribed, which is what guarantees the non-retained
        # publishes below are actually received.
        await _wait_until(lambda: mqtt.subscribed)
        prefix = f"vmc/{cfg.machine_id}"
        for name in ("vending", "mdb", "ice_maker"):
            await probe.publish(
                f"{prefix}/heartbeat/{name}",
                json.dumps({"subsystem": name, "uptime_seconds": 1}),
            )
        await probe.publish(
            f"{prefix}/payment/status",
            json.dumps({"device": "coin_acceptor", "state": "ready"}),
        )
        await probe.publish(
            f"{prefix}/hardware/io/bin_half_full",
            json.dumps({"device": "bin_half_full", "state": True}),
        )
        # Wait on the state the three publishes above (heartbeats for
        # vending/mdb/ice_maker, payment/status, bin_half_full) produce --
        # everything alive -- rather than a fixed sleep.
        await _wait_until(
            lambda: avail.payment_enabled is True
            and avail.sale_available("ice")[0] is True
        )
        await probe.publish(
            f"{prefix}/heartbeat/vending",
            json.dumps({"subsystem": "vending", "uptime_seconds": -1}),
        )
        await _wait_until(lambda: "vending_alive" in avail.sale_available("ice")[1])
        assert avail.payment_enabled is True
        sellable, reasons = avail.sale_available("ice")
        assert sellable is False
        assert "vending_alive" in reasons

        await probe.publish(
            f"{prefix}/heartbeat/vending",
            json.dumps({"subsystem": "vending", "uptime_seconds": 2}),
        )
        await _wait_until(lambda: avail.sale_available("ice")[0] is True)
        assert avail.sale_available("ice")[0] is True

        while True:
            try:
                msg = await asyncio.wait_for(probe.messages.__anext__(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            seen.append(json.loads(msg.payload)["accept"])

    await _stop_background_task(run)
    machine.cancel_pending_tasks()
    assert seen  # payment/enable was published at least once (initial true)
    assert all(seen), "payment/enable must never go false on fulfillment-only loss"
