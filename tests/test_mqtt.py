# tests/test_mqtt.py
"""Tests for MQTT message schemas, client topic matching, and VMC MQTT wiring."""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest
from pydantic import ValidationError

from config.config_model import ConfigModel, MQTTConfig, Product
from controller.vmc import VMC
from services.mqtt_client import (
    MQTTClient,
    PROTOCOL_VERSIONS,
    DEPRECATED_PROTOCOL_VERSIONS,
)
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
        s = VMCStatus(
            state="dispensing",
            credit_escrow=2.50,
            selected_product="Ice 10lb",
            uptime_seconds=300,
        )
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
        assert (
            MQTTClient._topic_matches("sensors/temp/+", "sensors/temp/evaporator")
            is True
        )

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
    async def test_publish_defaults_to_qos_1(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        client._client = AsyncMock()
        client._connected = True

        await client.publish("status", {"state": "idle"})

        client._client.publish.assert_awaited_once()
        _, kwargs = client._client.publish.call_args
        assert kwargs["qos"] == 1

    @pytest.mark.asyncio
    async def test_publish_honors_explicit_qos_0(self):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        client._client = AsyncMock()
        client._connected = True

        await client.publish("sensors/temp/evaporator", {"value": -12.5}, qos=0)

        client._client.publish.assert_awaited_once()
        _, kwargs = client._client.publish.call_args
        assert kwargs["qos"] == 0

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
        handler.assert_awaited_once_with(
            "payment/credit", {"amount": 1.0, "method": "cash"}
        )

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


class _FakeAiomqttClient:
    """Stand-in for aiomqtt.Client: records subscribe() calls, yields no messages."""

    def __init__(self, *args, **kwargs):
        self.subscribed: list[tuple[str, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    async def publish(self, topic, payload, qos=0, retain=False):
        pass

    async def _empty_messages(self):
        return
        yield  # pragma: no cover - never reached; makes this an async generator

    @property
    def messages(self):
        return self._empty_messages()


class TestMQTTClientSubscribeQoS:
    @pytest.mark.asyncio
    async def test_connect_and_listen_subscribes_at_qos_1(self, monkeypatch):
        cfg = MQTTConfig()
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        client.register("payment/credit", AsyncMock())
        client.register("sensors/temp/+", AsyncMock())

        fake = _FakeAiomqttClient()
        monkeypatch.setattr(
            "services.mqtt_client.aiomqtt.Client", lambda *a, **kw: fake
        )

        await client._connect_and_listen()

        assert fake.subscribed == [
            ("vmc/vmc-0001/payment/credit", 1),
            ("vmc/vmc-0001/sensors/temp/+", 1),
        ]


class TestMQTTProtocolVersion:
    """The broker connection must negotiate MQTT v5 unless config says 3.1.1."""

    @staticmethod
    async def _connect_with(cfg, monkeypatch) -> dict:
        captured: dict = {}
        fake = _FakeAiomqttClient()

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            return fake

        monkeypatch.setattr("services.mqtt_client.aiomqtt.Client", _factory)
        client = MQTTClient(config=cfg, machine_id="vmc-0001")
        await client._connect_and_listen()
        return captured

    @pytest.mark.asyncio
    async def test_defaults_to_v5(self, monkeypatch):
        captured = await self._connect_with(MQTTConfig(), monkeypatch)
        assert captured["protocol"] is aiomqtt.ProtocolVersion.V5

    @pytest.mark.asyncio
    async def test_honors_configured_v311(self, monkeypatch):
        cfg = MQTTConfig(protocol_version="3.1.1")
        captured = await self._connect_with(cfg, monkeypatch)
        assert captured["protocol"] is aiomqtt.ProtocolVersion.V311

    def test_rejects_unknown_version(self):
        with pytest.raises(ValidationError):
            MQTTConfig(protocol_version="3.1")

    def test_every_accepted_version_maps_to_an_enum(self):
        accepted = MQTTConfig.model_fields["protocol_version"].annotation.__args__
        assert set(accepted) == set(PROTOCOL_VERSIONS)

    def test_default_is_not_deprecated(self):
        assert MQTTConfig().protocol_version not in DEPRECATED_PROTOCOL_VERSIONS

    @pytest.mark.asyncio
    async def test_v311_logs_a_deprecation_warning(self, monkeypatch, caplog):
        cfg = MQTTConfig(protocol_version="3.1.1")
        with caplog.at_level("WARNING"):
            await self._connect_with(cfg, monkeypatch)
        assert "deprecated" in caplog.text
        assert "2027.01" in caplog.text


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
        assert mock_client.register.call_count == 12

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

        await vmc._handle_mqtt_payment(
            "payment/credit", {"amount": 2.50, "method": "card"}
        )
        assert vmc.credit_escrow == 2.50
        assert vmc.last_payment_method == "card"

    @pytest.mark.asyncio
    async def test_handle_mqtt_button_selects_product(self):
        config = ConfigModel()
        config.physical.products = [Product(sku="T-1", name="Test", price=1.0)]
        vmc = VMC(config=config)
        loop = asyncio.get_running_loop()
        vmc.attach_to_loop(loop)

        # button 0 should select the first configured product
        await vmc._handle_mqtt_button("hardware/buttons", {"button": 0})
        assert vmc.selected_product is not None
        assert vmc.state == "interacting_with_user"


class TestMonitorContractHandlers:
    def _vmc_with_monitor(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        monitor = HealthMonitor()
        vmc.set_health_monitor(monitor)
        return vmc, monitor

    async def test_capabilities_stored(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_capabilities(
            "capabilities/ice_maker",
            {
                "subsystem": "ice_maker",
                "contract_version": CONTRACT_VERSION,
                "brand": "BrandX",
                "model": "IM-500",
                "firmware": "0.1.0",
                "channels": [],
                "commands": ["power_cycle"],
            },
        )
        assert "ice_maker" in vmc.subsystem_capabilities
        assert vmc.subsystem_capabilities["ice_maker"]["brand"] == "BrandX"

    async def test_malformed_capabilities_stored_raw_with_warning(self):
        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_capabilities(
            "capabilities/vending", {"subsystem": "vending", "whatever": 1}
        )
        assert vmc.subsystem_capabilities["vending"] == {
            "subsystem": "vending",
            "whatever": 1,
        }

    async def test_telemetry_routed_to_health_monitor(self):
        vmc, monitor = self._vmc_with_monitor()
        await vmc._handle_mqtt_telemetry(
            "telemetry/ice_maker/bin_level",
            {"channel_id": "bin_level", "value": 42.0},
        )
        assert monitor.get_summary()["channels"]["bin_level"]["value"] == 42.0

    async def test_lwt_heartbeat_marks_offline(self):
        vmc, monitor = self._vmc_with_monitor()
        await vmc._handle_mqtt_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": 10}
        )
        assert monitor.get_summary()["subsystems"]["ice_maker"]["alive"] is True
        await vmc._handle_mqtt_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": -1}
        )
        assert monitor.get_summary()["subsystems"]["ice_maker"]["alive"] is False

    async def test_command_ack_logged_without_error(self):
        vmc, _ = self._vmc_with_monitor()
        await vmc._handle_mqtt_command_ack(
            "cmd/ice_maker/ack",
            {
                "request_id": "req-00000001",
                "command": "power_cycle",
                "status": "ok",
            },
        )  # must not raise

    async def test_capabilities_forwarded_to_health_monitor(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        await vmc._handle_mqtt_capabilities(
            "capabilities/vending",
            {
                "subsystem": "vending",
                "firmware": "abc1234",
                "contract_version": "0.3.0",
                "hardware_id": "02:11:22:33:44:55",
                "future_field": "ignored",
            },
        )
        row = hm.get_summary()["subsystems"]["vending"]
        assert row["firmware"] == "abc1234"
        assert row["hardware_id"] == "02:11:22:33:44:55"
        assert row["alive"] is False

    async def test_malformed_capabilities_still_forwarded_raw(self):
        from services.health_monitor import HealthMonitor

        vmc = VMC(config=ConfigModel())
        hm = HealthMonitor()
        vmc.set_health_monitor(hm)
        await vmc._handle_mqtt_capabilities(
            "capabilities/mdb", {"subsystem": "mdb", "whatever": 1}
        )
        assert hm.get_summary()["subsystems"]["mdb"]["firmware"] is None


class _RecordingAiomqttClient(_FakeAiomqttClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.published: list[tuple] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class TestMQTTPresence:
    async def test_publish_forwards_retain(self):
        client = MQTTClient(config=MQTTConfig(), machine_id="vmc-0001")
        client._client = AsyncMock()
        client._connected = True
        await client.publish("status", {"a": 1}, retain=True)
        client._client.publish.assert_awaited_once_with(
            "vmc/vmc-0001/status", '{"a": 1}', qos=1, retain=True
        )

    async def test_connect_sets_last_will_and_publishes_online(self, monkeypatch):
        import services.mqtt_client as mc

        captured: dict = {}
        fake = _RecordingAiomqttClient()

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return fake

        monkeypatch.setattr(mc.aiomqtt, "Client", factory)
        client = MQTTClient(config=MQTTConfig(), machine_id="vmc-0001")
        await client._connect_and_listen()

        will = captured["will"]
        assert will.topic == "vmc/vmc-0001/online"
        assert will.retain is True
        assert will.qos == 1
        assert json.loads(will.payload)["online"] is False

        topic, payload, qos, retain = fake.published[0]
        assert topic == "vmc/vmc-0001/online"
        assert json.loads(payload)["online"] is True
        assert (qos, retain) == (1, True)

    def test_vmc_online_model(self):
        from services.mqtt_messages import VMCOnline

        m = VMCOnline(online=True)
        assert m.online is True
        assert isinstance(m.timestamp, datetime)


class TestStatusRetained:
    async def test_publish_status_is_retained(self):
        vmc = _make_vmc()
        vmc.attach_to_loop(asyncio.get_running_loop())
        mqtt = MagicMock()
        mqtt.publish = AsyncMock()
        vmc._mqtt_client = mqtt
        vmc._publish_status()
        await asyncio.sleep(0)
        args, kwargs = mqtt.publish.await_args
        assert args[0] == "status"
        assert kwargs.get("retain") is True
        vmc.cancel_pending_tasks()
