"""Unit tests for ice_edge/hal/mqtt_inbound.py's TelemetryRouter.

The router validates and fans out; these tests attach recording subscribers
and check each topic reaches them with the documented arguments.
`TestSubscriptionsTable` at the bottom imports VMC purely to check
SUBSCRIPTIONS names real attributes.
"""

import pytest

from ice_contracts.board.ice_maker_monitor import CONTRACT_VERSION
from ice_edge.hal.mqtt_inbound import SUBSCRIPTIONS, TelemetryRouter


def _caps_payload(subsystem="ice_maker"):
    return {
        "subsystem": subsystem,
        "contract_version": CONTRACT_VERSION,
        "brand": "BrandX",
        "model": "IM-500",
        "firmware": "0.1.0",
        "channels": [],
        "commands": ["power_cycle"],
    }


class TestHardwareIO:
    async def test_publishes_device_and_state(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_hardware_io(lambda d, s: seen.append((d, s)))
        await router.handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )
        assert seen == [("door", True)]

    async def test_invalid_payload_reaches_nobody(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_hardware_io(lambda d, s: seen.append((d, s)))
        with pytest.raises(Exception):
            await router.handle_hardware_io("hardware/io/door", {"device": "door"})
        assert seen == []

    async def test_no_subscribers_is_fine(self):
        await TelemetryRouter().handle_hardware_io(
            "hardware/io/door", {"device": "door", "state": True}
        )


class TestPaymentStatus:
    async def test_publishes_device_and_state(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_payment_status(lambda d, s: seen.append((d, s)))
        await router.handle_payment_status(
            "payment/status", {"device": "coin_acceptor", "state": "ready"}
        )
        assert seen == [("coin_acceptor", "ready")]

    async def test_invalid_payload_reaches_nobody(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_payment_status(lambda d, s: seen.append((d, s)))
        with pytest.raises(Exception):
            await router.handle_payment_status("payment/status", {"device": "x"})
        assert seen == []


class TestSensor:
    async def test_publishes_location_and_float_value(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_sensor(lambda loc, v: seen.append((loc, v)))
        await router.handle_sensor(
            "sensors/temp/freezer", {"location": "freezer", "value": -5}
        )
        assert seen == [("freezer", -5.0)]
        assert isinstance(seen[0][1], float)

    async def test_location_falls_back_to_topic_tail(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_sensor(lambda loc, v: seen.append((loc, v)))
        await router.handle_sensor("sensors/temp/freezer", {"value": 1.5})
        assert seen == [("freezer", 1.5)]

    async def test_missing_value_reaches_nobody(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_sensor(lambda loc, v: seen.append((loc, v)))
        await router.handle_sensor("sensors/temp/freezer", {"location": "freezer"})
        assert seen == []


class TestHeartbeat:
    async def test_normal_uptime_is_alive(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_heartbeat(lambda s, d, a: seen.append((s, d, a)))
        payload = {"subsystem": "ice_maker", "uptime_seconds": 10}
        await router.handle_heartbeat("heartbeat/ice_maker", payload)
        assert seen == [("ice_maker", payload, True)]

    async def test_uptime_minus_one_is_last_will(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_heartbeat(lambda s, d, a: seen.append((s, d, a)))
        payload = {"subsystem": "ice_maker", "uptime_seconds": -1}
        await router.handle_heartbeat("heartbeat/ice_maker", payload)
        assert seen == [("ice_maker", payload, False)]


class TestIceMakerEvent:
    async def test_publishes_validated_event(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_ice_maker_event(seen.append)
        await router.handle_ice_maker_event("ice_maker/event", {"event": "power_on"})
        assert [e.event for e in seen] == ["power_on"]

    async def test_invalid_payload_reaches_nobody(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_ice_maker_event(seen.append)
        with pytest.raises(Exception):
            await router.handle_ice_maker_event("ice_maker/event", {})
        assert seen == []


class TestTelemetry:
    async def test_publishes_channel_and_value(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_telemetry(lambda c, v: seen.append((c, v)))
        await router.handle_telemetry(
            "telemetry/ice_maker/bin_level", {"channel_id": "bin_level", "value": 42.0}
        )
        assert seen == [("bin_level", 42.0)]

    async def test_invalid_payload_reaches_nobody(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_telemetry(lambda c, v: seen.append((c, v)))
        with pytest.raises(Exception):
            await router.handle_telemetry("telemetry/ice_maker/x", {"value": 1})
        assert seen == []


class TestCapabilities:
    async def test_valid_doc_stores_and_publishes_with_caps(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_capabilities(lambda s, d, c: seen.append((s, d, c)))
        payload = _caps_payload()
        await router.handle_capabilities("capabilities/ice_maker", payload)
        assert router.capabilities["ice_maker"] == payload
        assert len(seen) == 1
        assert seen[0][0] == "ice_maker" and seen[0][1] == payload
        assert seen[0][2].firmware == "0.1.0"

    async def test_malformed_doc_stores_raw_and_publishes_none_caps(self):
        router, seen = TelemetryRouter(), []
        router.subscribe_capabilities(lambda s, d, c: seen.append((s, d, c)))
        payload = {"subsystem": "vending", "whatever": 1}
        await router.handle_capabilities("capabilities/vending", payload)
        assert router.capabilities["vending"] == payload
        assert seen == [("vending", payload, None)]

    async def test_two_subscribers_receive_in_order(self):
        router, order = TelemetryRouter(), []
        router.subscribe_capabilities(lambda s, d, c: order.append("first"))
        router.subscribe_capabilities(lambda s, d, c: order.append("second"))
        await router.handle_capabilities("capabilities/mdb", _caps_payload("mdb"))
        assert order == ["first", "second"]

    async def test_late_subscriber_gets_replay(self):
        router, seen = TelemetryRouter(), []
        payload = _caps_payload("vending")
        await router.handle_capabilities("capabilities/vending", payload)
        router.subscribe_capabilities(lambda s, d, c: seen.append((s, d, c)))
        assert len(seen) == 1
        assert seen[0][0] == "vending" and seen[0][2] is not None

    async def test_replay_goes_only_to_the_new_subscriber(self):
        router, early, late = TelemetryRouter(), [], []
        router.subscribe_capabilities(lambda s, d, c: early.append(s))
        await router.handle_capabilities("capabilities/mdb", _caps_payload("mdb"))
        router.subscribe_capabilities(lambda s, d, c: late.append(s))
        assert early == ["mdb"]
        assert late == ["mdb"]


class TestCommandAck:
    async def test_logs_without_error(self):
        await TelemetryRouter().handle_command_ack(
            "cmd/ice_maker/ack",
            {"request_id": "req-00000001", "command": "power_cycle", "status": "ok"},
        )


class TestSubscriptionsTable:
    def test_every_vmc_owner_method_is_a_vmc_attribute(self):
        from ice_edge.sale.vmc import VMC

        for _, owner, name in SUBSCRIPTIONS:
            if owner == "vmc":
                assert hasattr(VMC, name), f"VMC has no attribute {name!r}"

    def test_every_telemetry_owner_method_is_a_router_attribute(self):
        for _, owner, name in SUBSCRIPTIONS:
            if owner == "telemetry":
                assert hasattr(TelemetryRouter, name), (
                    f"TelemetryRouter has no attribute {name!r}"
                )

    def test_subscriptions_table(self):
        # The full (topic, owner, method) triple, in order, so a transposition
        # fails even though every method name would still be valid alone.
        # sensors/water_flow is gone (spec 3.4).
        assert SUBSCRIPTIONS == (
            ("payment/credit", "vmc", "on_payment_credit"),
            ("hardware/buttons", "vmc", "on_button_press"),
            ("hardware/dispenser", "vmc", "on_dispenser_event"),
            ("sensors/temp/+", "telemetry", "handle_sensor"),
            ("heartbeat/+", "telemetry", "handle_heartbeat"),
            ("ice_maker/event", "telemetry", "handle_ice_maker_event"),
            ("capabilities/+", "telemetry", "handle_capabilities"),
            ("telemetry/ice_maker/+", "telemetry", "handle_telemetry"),
            ("cmd/ice_maker/ack", "telemetry", "handle_command_ack"),
            ("hardware/io/+", "telemetry", "handle_hardware_io"),
            ("cmd/payment/refund/ack", "vmc", "on_refund_ack"),
            ("payment/status", "telemetry", "handle_payment_status"),
        )
        assert len(SUBSCRIPTIONS) == 12
