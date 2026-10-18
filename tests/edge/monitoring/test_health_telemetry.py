"""HealthTelemetry makes exactly the HealthMonitor calls the old router made."""

from ice_contracts.board.mqtt_messages import IceMakerEvent
from ice_edge.monitoring.health_telemetry import HealthTelemetry


class FakeHealth:
    def __init__(self):
        self.signals: list[tuple] = []
        self.temperatures: list[tuple] = []
        self.channels: list[tuple] = []
        self.heartbeats: list[tuple] = []
        self.offline: list[str] = []
        self.capabilities: list[tuple] = []

    def record_signal(self, subsystem, channel_id, value, *, text=None):
        self.signals.append((subsystem, channel_id, value, text))

    def record_temperature(self, location, value):
        self.temperatures.append((location, value))

    def record_channel(self, channel_id, value):
        self.channels.append((channel_id, value))

    def record_heartbeat(self, subsystem, payload=None):
        self.heartbeats.append((subsystem, payload))

    def mark_offline(self, subsystem):
        self.offline.append(subsystem)

    def record_capabilities(self, subsystem, caps):
        self.capabilities.append((subsystem, caps))


def _make():
    health = FakeHealth()
    return health, HealthTelemetry(lambda: health)


def test_hardware_io():
    health, ht = _make()
    ht.on_hardware_io("door", True)
    ht.on_hardware_io("door", False)
    assert health.signals == [
        ("vending", "door", 1.0, None),
        ("vending", "door", 0.0, None),
    ]


def test_payment_status_ready_and_error():
    health, ht = _make()
    ht.on_payment_status("coin_acceptor", "ready")
    ht.on_payment_status("bill_acceptor", "error")
    assert health.signals == [
        ("mdb", "coin_acceptor", 1.0, "ready"),
        ("mdb", "bill_acceptor", 0.0, "error"),
    ]


def test_sensor_and_telemetry():
    health, ht = _make()
    ht.on_sensor("freezer", -5.0)
    ht.on_telemetry("bin_level", 42.0)
    assert health.temperatures == [("freezer", -5.0)]
    assert health.channels == [("bin_level", 42.0)]


def test_heartbeat_alive_and_last_will():
    health, ht = _make()
    payload = {"subsystem": "ice_maker", "uptime_seconds": 10}
    ht.on_heartbeat("ice_maker", payload, True)
    ht.on_heartbeat("ice_maker", {"uptime_seconds": -1}, False)
    assert health.heartbeats == [("ice_maker", payload)]
    assert health.offline == ["ice_maker"]


def test_ice_maker_power_events_record_compressor_run():
    health, ht = _make()
    ht.on_ice_maker_event(IceMakerEvent(event="power_on"))
    ht.on_ice_maker_event(IceMakerEvent(event="power_off"))
    ht.on_ice_maker_event(IceMakerEvent(event="ice_dropped"))
    assert health.signals == [
        ("ice_maker", "compressor_run", 1.0, None),
        ("ice_maker", "compressor_run", 0.0, None),
    ]


def test_capabilities_records_raw_dict_even_when_caps_none():
    health, ht = _make()
    raw = {"subsystem": "vending", "whatever": 1}
    ht.on_capabilities("vending", raw, None)
    assert health.capabilities == [("vending", raw)]


def test_no_health_monitor_is_a_noop():
    ht = HealthTelemetry(lambda: None)
    ht.on_hardware_io("door", True)
    ht.on_payment_status("d", "ready")
    ht.on_sensor("x", 1.0)
    ht.on_heartbeat("s", {}, True)
    ht.on_heartbeat("s", {}, False)
    ht.on_ice_maker_event(IceMakerEvent(event="power_on"))
    ht.on_telemetry("c", 1.0)
    ht.on_capabilities("s", {}, None)
