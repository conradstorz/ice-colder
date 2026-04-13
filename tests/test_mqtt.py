# tests/test_mqtt.py
"""Tests for MQTT message schemas, client topic matching, and VMC MQTT wiring."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.config_model import ConfigModel, MQTTConfig
from controller.vmc import VMC
from services.mqtt_client import MQTTClient
from services.mqtt_messages import (
    ButtonPress,
    DispenseCommand,
    DisplayCommand,
    DisplayMode,
    DispenserStatus,
    PaymentEnableCommand,
    PaymentEvent,
    PaymentStatus,
    SensorReading,
    SubsystemHeartbeat,
    VMCAlert,
    VMCStatus,
    AlertLevel,
)


# ── Message schema tests ─────────────────────────────────────


class TestInboundSchemas:
    def test_sensor_reading_defaults(self):
        r = SensorReading(location="evaporator", value=-12.5)
        assert r.unit == "C"
        assert isinstance(r.timestamp, datetime)

    def test_payment_event_required_fields(self):
        e = PaymentEvent(amount=1.25, method="cash")
        assert e.amount == 1.25
        assert e.method == "cash"

    def test_button_press_defaults(self):
        b = ButtonPress(button=2)
        assert b.action == "pressed"

    def test_dispenser_status(self):
        d = DispenserStatus(slot=0, state="complete")
        assert d.slot == 0
        assert d.state == "complete"

    def test_payment_status(self):
        p = PaymentStatus(device="coin_acceptor", state="ready")
        assert p.device == "coin_acceptor"

    def test_heartbeat_defaults(self):
        h = SubsystemHeartbeat(subsystem="mdb")
        assert h.uptime_seconds == 0


class TestOutboundSchemas:
    def test_dispense_command(self):
        c = DispenseCommand(slot=3)
        assert c.slot == 3

    def test_payment_enable_command(self):
        c = PaymentEnableCommand(accept=True)
        assert c.accept is True

    def test_display_command_modes(self):
        for mode in DisplayMode:
            c = DisplayCommand(mode=mode)
            assert c.mode == mode


class TestStatusSchemas:
    def test_vmc_status_defaults(self):
        s = VMCStatus(state="idle")
        assert s.credit_escrow == 0.0
        assert s.selected_product is None
        assert s.uptime_seconds == 0

    def test_vmc_alert_levels(self):
        for level in AlertLevel:
            a = VMCAlert(level=level, message="test")
            assert a.level == level
            assert a.source == "vmc"

    def test_vmc_status_json_roundtrip(self):
        s = VMCStatus(state="dispensing", credit_escrow=2.50, selected_product="Ice 10lb", uptime_seconds=300)
        data = s.model_dump()
        s2 = VMCStatus.model_validate(data)
        assert s2.state == "dispensing"
        assert s2.credit_escrow == 2.50


# ── Topic matching tests ──────────────────────────────────────


class TestTopicMatching:
    def test_exact_match(self):
        assert MQTTClient._topic_matches("payment/credit", "payment/credit") is True

    def test_exact_no_match(self):
        assert MQTTClient._topic_matches("payment/credit", "payment/status") is False

    def test_single_level_wildcard(self):
        assert MQTTClient._topic_matches("sensors/temp/+", "sensors/temp/evaporator") is True

    def test_single_level_wildcard_wrong_depth(self):
        assert MQTTClient._topic_matches("sensors/temp/+", "sensors/temp/a/b") is False

    def test_multi_level_wildcard(self):
        assert MQTTClient._topic_matches("sensors/#", "sensors/temp/evaporator") is True

    def test_multi_level_wildcard_root(self):
        assert MQTTClient._topic_matches("#", "anything/goes/here") is True

    def test_shorter_pattern_no_match(self):
        assert MQTTClient._topic_matches("sensors/temp", "sensors/temp/extra") is False

    def test_longer_pattern_no_match(self):
        assert MQTTClient._topic_matches("sensors/temp/extra", "sensors/temp") is False


# ── MQTTClient unit tests ────────────────────────────────────


class TestMQTTClientUnit:
    def test_topic_prefix(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        assert client.topic_prefix == "vmc/vmc-0001"

    def test_register_adds_handler(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        handler = AsyncMock()
        client.register("test/topic", handler)
        assert len(client._handlers) == 1
        assert client._handlers[0] == ("test/topic", handler)

    def test_not_connected_initially(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_publish_when_not_connected_does_nothing(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        # Should not raise
        await client.publish("status", {"state": "idle"})

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_handler(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="test-machine")
        handler = AsyncMock()
        client.register("payment/credit", handler)

        # Create a fake MQTT message
        msg = MagicMock()
        msg.topic = MagicMock()
        msg.topic.__str__ = lambda self: "vmc/test-machine/payment/credit"
        msg.payload = b'{"amount": 1.0, "method": "cash"}'

        await client._dispatch(msg)
        handler.assert_awaited_once_with("payment/credit", {"amount": 1.0, "method": "cash"})

    @pytest.mark.asyncio
    async def test_dispatch_ignores_wrong_prefix(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="test-machine")
        handler = AsyncMock()
        client.register("payment/credit", handler)

        msg = MagicMock()
        msg.topic = MagicMock()
        msg.topic.__str__ = lambda self: "vmc/other-machine/payment/credit"
        msg.payload = b'{"amount": 1.0}'

        await client._dispatch(msg)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_handles_invalid_json(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="test-machine")
        handler = AsyncMock()
        client.register("payment/credit", handler)

        msg = MagicMock()
        msg.topic = MagicMock()
        msg.topic.__str__ = lambda self: "vmc/test-machine/payment/credit"
        msg.payload = b"not json"

        await client._dispatch(msg)
        handler.assert_not_awaited()


# ── VMC MQTT wiring tests ────────────────────────────────────


def _make_vmc():
    config = ConfigModel()
    vmc = VMC(config=config)
    return vmc


class TestVMCMQTTWiring:
    def test_set_mqtt_client_registers_handlers(self):
        vmc = _make_vmc()
        mock_client = MagicMock()
        vmc.set_mqtt_client(mock_client)
        assert mock_client.register.call_count == 5

    def test_publish_status_without_client_does_nothing(self):
        vmc = _make_vmc()
        # Should not raise when no client attached
        vmc._publish_status()

    def test_publish_status_without_loop_does_nothing(self):
        vmc = _make_vmc()
        vmc._mqtt_client = MagicMock()
        # _loop is None
        vmc._publish_status()

    @pytest.mark.asyncio
    async def test_handle_mqtt_payment_deposits_funds(self):
        vmc = _make_vmc()
        loop = asyncio.get_running_loop()
        vmc.attach_to_loop(loop)
        vmc.start_interaction()

        await vmc._handle_mqtt_payment("payment/credit", {"amount": 2.50, "method": "card"})
        assert vmc.credit_escrow == 2.50
        assert vmc.last_payment_method == "card"

    @pytest.mark.asyncio
    async def test_handle_mqtt_button_selects_product(self):
        vmc = _make_vmc()
        loop = asyncio.get_running_loop()
        vmc.attach_to_loop(loop)

        # VMC starts idle with default products; button 0 should select first product
        await vmc._handle_mqtt_button("hardware/buttons", {"button": 0})
        assert vmc.selected_product is not None
        assert vmc.state == "interacting_with_user"
