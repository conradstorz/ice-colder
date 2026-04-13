# tests/test_integration_e2e.py
"""
End-to-end integration tests using a real MQTT broker (mosquitto).

These tests exercise the full transaction loop:
  button press → product selection → payment → dispense command → complete → idle

Requirements:
  - mosquitto running on localhost:1883 (docker compose -f docker/docker-compose.yml up -d)

Tests are skipped automatically if the broker is unreachable.
"""
import asyncio
import json
import sys

import pytest

# Windows needs SelectorEventLoop for aiomqtt
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiomqtt
    _BROKER_AVAILABLE = None  # checked at module scope below
except ImportError:
    _BROKER_AVAILABLE = False


async def _check_broker():
    """Return True if mosquitto is reachable on localhost:1883."""
    try:
        async with aiomqtt.Client(hostname="localhost", port=1883) as client:
            await client.publish("test/ping", "ok")
        return True
    except Exception:
        return False


if _BROKER_AVAILABLE is None:
    _BROKER_AVAILABLE = asyncio.run(_check_broker())

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _BROKER_AVAILABLE, reason="MQTT broker not available on localhost:1883"),
]


from config.config_model import ConfigModel, Product
from controller.vmc import VMC
from services.mqtt_client import MQTTClient
from services.mqtt_messages import (
    ButtonPress,
    DispenseCommand,
    DispenserStatus,
    PaymentEvent,
    SensorReading,
)
from services.health_monitor import HealthMonitor


def _make_config() -> ConfigModel:
    """Create a minimal config for testing with known products."""
    config = ConfigModel(
        machine_id="test-e2e",
        physical={
            "common_name": "E2E Test Machine",
            "serial_number": "test-e2e",
            "products": [
                {"sku": "ICE-SM", "name": "Small Ice", "price": 2.00},
                {"sku": "ICE-LG", "name": "Large Ice", "price": 3.50},
                {"sku": "WATER", "name": "Purified Water", "price": 1.50},
            ],
        },
        mqtt={
            "broker_host": "localhost",
            "broker_port": 1883,
            "client_id": "e2e-test-vmc",
            "reconnect_interval": 1.0,
        },
    )
    return config


async def _wait_for_state(vmc: VMC, target_state: str, timeout: float = 10.0):
    """Poll VMC state until it reaches target_state or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if vmc.state == target_state:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"VMC did not reach state '{target_state}' within {timeout}s (current: {vmc.state})")


class TestFullTransactionLoop:
    """Test the complete vend cycle through real MQTT."""

    async def test_button_payment_dispense_complete(self):
        """Full happy path: button → payment → dispense → complete → idle."""
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        # Set up VMC with real MQTT client
        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)
        health = HealthMonitor()

        # We need a separate "simulator" MQTT client to inject messages
        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-simulator"
        ) as sim_client:
            # Subscribe to dispense commands so we can react
            await sim_client.subscribe(f"{prefix}/cmd/dispense")

            # Start the VMC MQTT client in background
            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)
            vmc.set_health_monitor(health)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                # Wait for MQTT client to connect
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)
                assert mqtt_client.connected, "MQTT client failed to connect"

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

                # 3. Verify dispense command was published
                dispense_msg = None
                try:
                    msg = await asyncio.wait_for(
                        sim_client.messages.__anext__(), timeout=5.0
                    )
                    dispense_msg = json.loads(msg.payload)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass
                assert dispense_msg is not None, "No dispense command received"
                assert dispense_msg["slot"] == 0

                # 4. Simulate dispenser completing
                await sim_client.publish(
                    f"{prefix}/hardware/dispenser",
                    DispenserStatus(slot=0, state="complete").model_dump_json(),
                )

                # VMC should return to idle (no remaining credit)
                await _wait_for_state(vmc, "idle", timeout=5.0)
                assert vmc.credit_escrow == 0.0

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_overpayment_returns_to_idle_with_change(self):
        """Overpayment: VMC dispenses and remaining credit is consumed or idle."""
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-sim-overpay"
        ) as sim_client:

            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)

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

                # Dispenser completes
                await sim_client.publish(
                    f"{prefix}/hardware/dispenser",
                    DispenserStatus(slot=2, state="complete").model_dump_json(),
                )

                # With remaining credit, VMC goes back to interacting_with_user
                await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)
                assert abs(vmc.credit_escrow - 3.50) < 0.01

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_insufficient_funds_waits_for_more(self):
        """Underpayment: VMC stays in interacting_with_user until enough is deposited."""
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-sim-underpay"
        ) as sim_client:

            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)

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
                await asyncio.sleep(0.5)
                # Should still be interacting, not dispensing
                assert vmc.state == "interacting_with_user"
                assert abs(vmc.credit_escrow - 1.00) < 0.01

                # Insert another $3.00 — now $4.00 total >= $3.50
                await sim_client.publish(
                    f"{prefix}/payment/credit",
                    PaymentEvent(amount=3.00, method="cash_bill").model_dump_json(),
                )

                # Now it should transition to dispensing
                await _wait_for_state(vmc, "dispensing", timeout=10.0)
                # Credit should be $4.00 - $3.50 = $0.50
                assert abs(vmc.credit_escrow - 0.50) < 0.01

                # Complete the dispense
                await sim_client.publish(
                    f"{prefix}/hardware/dispenser",
                    DispenserStatus(slot=1, state="complete").model_dump_json(),
                )
                # With $0.50 remaining, goes back to interacting
                await _wait_for_state(vmc, "interacting_with_user", timeout=5.0)

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_dispenser_error_transitions_to_error_state(self):
        """Dispenser jam/error during vend puts VMC in error state."""
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-sim-error"
        ) as sim_client:

            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)

                # Button + payment
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

                # Dispenser reports error instead of complete
                await sim_client.publish(
                    f"{prefix}/hardware/dispenser",
                    DispenserStatus(slot=0, state="jammed").model_dump_json(),
                )
                await _wait_for_state(vmc, "error", timeout=5.0)

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass


class TestSensorAndHeartbeatRouting:
    """Test that sensor readings and heartbeats flow through to HealthMonitor."""

    async def test_sensor_readings_reach_health_monitor(self):
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)
        health = HealthMonitor()

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-sim-sensor"
        ) as sim_client:

            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)
            vmc.set_health_monitor(health)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)

                # Publish a temperature reading
                await sim_client.publish(
                    f"{prefix}/sensors/temp/compressor",
                    SensorReading(location="compressor", value=65.3).model_dump_json(),
                )
                await asyncio.sleep(0.5)

                summary = health.get_summary()
                assert "compressor" in summary["temperatures"]
                assert abs(summary["temperatures"]["compressor"]["value"] - 65.3) < 0.1

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_heartbeat_reaches_health_monitor(self):
        config = _make_config()
        prefix = f"vmc/{config.machine_id}"

        mqtt_client = MQTTClient(config=config.mqtt, machine_id=config.machine_id)
        vmc = VMC(config=config)
        health = HealthMonitor()

        async with aiomqtt.Client(
            hostname="localhost", port=1883, identifier="e2e-sim-hb"
        ) as sim_client:

            loop = asyncio.get_event_loop()
            vmc.attach_to_loop(loop)
            vmc.set_mqtt_client(mqtt_client)
            vmc.set_health_monitor(health)

            mqtt_task = asyncio.create_task(mqtt_client.run())

            try:
                for _ in range(50):
                    if mqtt_client.connected:
                        break
                    await asyncio.sleep(0.1)

                # Publish a heartbeat
                await sim_client.publish(
                    f"{prefix}/heartbeat/ice_maker",
                    json.dumps({"subsystem": "ice_maker", "uptime_seconds": 120}),
                )
                await asyncio.sleep(0.5)

                summary = health.get_summary()
                assert "ice_maker" in summary["subsystems"]

            finally:
                mqtt_task.cancel()
                try:
                    await mqtt_task
                except (asyncio.CancelledError, Exception):
                    pass
