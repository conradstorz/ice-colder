"""Unit tests for controller/mqtt_inbound.py's TelemetryRouter.

These exercise the router in isolation -- no VMC instance -- with small
fake health-monitor/availability objects that record calls, matching the
pattern this task's brief asked for. `TestSubscriptionsTable` at the bottom
is the one test that does import VMC, purely to check SUBSCRIPTIONS names
real VMC attributes and that the registered topic set hasn't changed.
"""

from contracts.ice_maker_monitor import CONTRACT_VERSION
from controller.mqtt_inbound import SUBSCRIPTIONS, TelemetryRouter


class FakeHealth:
    """Records every call a real HealthMonitor would have acted on."""

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


class FakeAvailability:
    """Records every call a real Availability would have acted on."""

    def __init__(self):
        self.hardware_io: list[tuple] = []
        self.payment_devices: list[tuple] = []

    def set_hardware_io(self, device, state):
        self.hardware_io.append((device, state))

    def set_payment_device(self, device, state):
        self.payment_devices.append((device, state))


def _router(
    *,
    health=None,
    availability=None,
    capabilities=None,
    on_bin_half_full=None,
    on_capabilities_validated=None,
):
    return TelemetryRouter(
        health=lambda: health,
        availability=lambda: availability,
        capabilities=capabilities if capabilities is not None else {},
        on_bin_half_full=on_bin_half_full or (lambda: None),
        on_capabilities_validated=on_capabilities_validated or (lambda s, c: None),
    )


class TestHardwareIO:
    async def test_forwards_to_availability_and_records_vending_signal(self):
        health, avail = FakeHealth(), FakeAvailability()
        router = _router(health=health, availability=avail)
        await router.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )
        assert avail.hardware_io == [("door", True)]
        assert health.signals == [("vending", "door", 1.0, None)]

    async def test_bin_half_full_true_fires_callback(self):
        calls = []
        router = _router(on_bin_half_full=lambda: calls.append(True))
        await router.handle_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
        )
        assert calls == [True]

    async def test_bin_half_full_false_does_not_fire_callback(self):
        calls = []
        router = _router(on_bin_half_full=lambda: calls.append(True))
        await router.handle_hardware_io(
            "hardware/io/bin_half_full", {"device": "bin_half_full", "state": False}
        )
        assert calls == []

    async def test_other_device_true_does_not_fire_callback(self):
        calls = []
        router = _router(on_bin_half_full=lambda: calls.append(True))
        await router.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )
        assert calls == []

    async def test_noop_without_health_or_availability(self):
        router = _router()
        await router.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )  # must not raise


class TestPaymentStatus:
    async def test_records_mdb_signal_with_text_and_feeds_availability(self):
        health, avail = FakeHealth(), FakeAvailability()
        router = _router(health=health, availability=avail)
        await router.handle_payment_status(
            "payment/status", {"device": "coin_acceptor", "state": "ready"}
        )
        assert avail.payment_devices == [("coin_acceptor", "ready")]
        assert health.signals == [("mdb", "coin_acceptor", 1.0, "ready")]

    async def test_not_ready_records_zero(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_payment_status(
            "payment/status", {"device": "bill_acceptor", "state": "error"}
        )
        assert health.signals == [("mdb", "bill_acceptor", 0.0, "error")]

    async def test_noop_without_health_or_availability(self):
        router = _router()
        await router.handle_payment_status(
            "payment/status", {"device": "coin_acceptor", "state": "ready"}
        )  # must not raise


class TestSensor:
    async def test_records_temperature(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_sensor(
            "sensors/temp/freezer", {"location": "freezer", "value": -5.0}
        )
        assert health.temperatures == [("freezer", -5.0)]

    async def test_noop_without_health(self):
        router = _router()
        await router.handle_sensor(
            "sensors/temp/freezer", {"location": "freezer", "value": -5.0}
        )  # must not raise


class TestWaterFlow:
    async def test_records_water_flow_channel(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_water_flow(
            "sensors/water_flow", {"location": "water_flow", "value": 1.2}
        )
        assert health.channels == [("water_flow", 1.2)]

    async def test_noop_without_health(self):
        router = _router()
        await router.handle_water_flow(
            "sensors/water_flow", {"location": "water_flow", "value": 1.2}
        )  # must not raise


class TestHeartbeat:
    async def test_uptime_minus_one_marks_offline(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": -1}
        )
        assert health.offline == ["ice_maker"]
        assert health.heartbeats == []

    async def test_normal_uptime_records_heartbeat(self):
        health = FakeHealth()
        router = _router(health=health)
        payload = {"subsystem": "ice_maker", "uptime_seconds": 10}
        await router.handle_heartbeat("heartbeat/ice_maker", payload)
        assert health.heartbeats == [("ice_maker", payload)]
        assert health.offline == []

    async def test_noop_without_health(self):
        router = _router()
        await router.handle_heartbeat(
            "heartbeat/ice_maker", {"subsystem": "ice_maker", "uptime_seconds": 10}
        )  # must not raise


class TestCapabilities:
    def _valid_payload(self, subsystem="ice_maker"):
        return {
            "subsystem": subsystem,
            "contract_version": CONTRACT_VERSION,
            "brand": "BrandX",
            "model": "IM-500",
            "firmware": "0.1.0",
            "channels": [],
            "commands": ["power_cycle"],
        }

    async def test_valid_doc_stores_records_and_fires_callback(self):
        health = FakeHealth()
        store: dict[str, dict] = {}
        calls = []
        router = _router(
            health=health,
            capabilities=store,
            on_capabilities_validated=lambda s, c: calls.append((s, c)),
        )
        payload = self._valid_payload()
        await router.handle_capabilities("capabilities/ice_maker", payload)
        assert store["ice_maker"] == payload
        assert health.capabilities == [("ice_maker", payload)]
        assert len(calls) == 1
        assert calls[0][0] == "ice_maker"
        assert calls[0][1].firmware == "0.1.0"

    async def test_malformed_doc_stores_raw_and_skips_callback(self):
        health = FakeHealth()
        store: dict[str, dict] = {}
        calls = []
        router = _router(
            health=health,
            capabilities=store,
            on_capabilities_validated=lambda s, c: calls.append((s, c)),
        )
        payload = {"subsystem": "vending", "whatever": 1}
        await router.handle_capabilities("capabilities/vending", payload)
        assert store["vending"] == payload
        assert health.capabilities == [("vending", payload)]
        assert calls == []

    async def test_noop_without_health(self):
        store: dict[str, dict] = {}
        router = _router(capabilities=store)
        payload = self._valid_payload(subsystem="mdb")
        await router.handle_capabilities("capabilities/mdb", payload)
        assert store["mdb"] == payload  # must not raise


class TestIceMakerEvent:
    async def test_power_on_records_compressor_run_high(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_ice_maker_event("ice_maker/event", {"event": "power_on"})
        assert health.signals == [("ice_maker", "compressor_run", 1.0, None)]

    async def test_power_off_records_compressor_run_low(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_ice_maker_event("ice_maker/event", {"event": "power_off"})
        assert health.signals == [("ice_maker", "compressor_run", 0.0, None)]

    async def test_other_event_does_not_record_signal(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_ice_maker_event("ice_maker/event", {"event": "ice_dropped"})
        assert health.signals == []

    async def test_noop_without_health(self):
        router = _router()
        await router.handle_ice_maker_event(
            "ice_maker/event", {"event": "power_on"}
        )  # must not raise


class TestTelemetry:
    async def test_records_channel(self):
        health = FakeHealth()
        router = _router(health=health)
        await router.handle_telemetry(
            "telemetry/ice_maker/bin_level", {"channel_id": "bin_level", "value": 42.0}
        )
        assert health.channels == [("bin_level", 42.0)]

    async def test_noop_without_health(self):
        router = _router()
        await router.handle_telemetry(
            "telemetry/ice_maker/bin_level", {"channel_id": "bin_level", "value": 42.0}
        )  # must not raise


class TestCommandAck:
    async def test_logs_without_error(self):
        router = _router()
        await router.handle_command_ack(
            "cmd/ice_maker/ack",
            {"request_id": "req-00000001", "command": "power_cycle", "status": "ok"},
        )  # must not raise


class TestSubscriptionsTable:
    def test_every_name_is_a_vmc_attribute(self):
        from controller.vmc import VMC

        for _, name in SUBSCRIPTIONS:
            assert hasattr(VMC, name), f"VMC has no attribute {name!r}"

    def test_subscriptions_table_unchanged(self):
        # The full (topic, method) pairing, in order -- not just the topic
        # set -- so a transposition (e.g. payment/status paired with
        # on_water_flow) fails this test even though both the
        # topic set and every method name would still be valid on their own.
        assert SUBSCRIPTIONS == (
            ("payment/credit", "on_payment_credit"),
            ("hardware/buttons", "on_button_press"),
            ("hardware/dispenser", "on_dispenser_event"),
            ("sensors/temp/+", "on_sensor_reading"),
            ("heartbeat/+", "on_heartbeat"),
            ("ice_maker/event", "on_ice_maker_event"),
            ("capabilities/+", "on_capabilities"),
            ("telemetry/ice_maker/+", "on_telemetry"),
            ("cmd/ice_maker/ack", "on_command_ack"),
            ("hardware/io/+", "on_hardware_io"),
            ("cmd/payment/refund/ack", "on_refund_ack"),
            ("payment/status", "on_payment_status"),
            ("sensors/water_flow", "on_water_flow"),
        )
        assert len(SUBSCRIPTIONS) == 13
