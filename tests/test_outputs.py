# tests/test_outputs.py
"""Unit tests for `controller.outputs.StatusOutputs`, in isolation from
`VMC` -- no FSM. Every sink (MQTT, health, availability, session store,
display) is a small fake that records what it was given; `tasks` is the
real `tests.fakes.FakeTaskRunner`, attached to the running event loop so
`fire_and_forget` publishes/persists actually run (and can be awaited with
a bare `asyncio.sleep(0)`) instead of being swallowed.
"""

from __future__ import annotations

import asyncio

from contracts.vending_machine import PaymentRefundCommand
from controller.outputs import StatusOutputs
from services.mqtt_messages import AlertLevel, PaymentEnableCommand, VMCAlert, VMCStatus
from services.payment_gateway_manager import PaymentGatewayManager
from services.session_store import SessionSnapshot
from tests.fakes import FakeTaskRunner


class FakeMqtt:
    """Records every publish() call; `register` is accepted and ignored
    since StatusOutputs never calls it, mirroring the real client's
    surface closely enough for other fakes in this test suite."""

    def __init__(self):
        self.published: list[tuple[str, object, bool]] = []

    async def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload, retain))


class FakeStore:
    def __init__(self, *, clear_result: bool = True):
        self.saved: list[SessionSnapshot] = []
        self.cleared_async_calls = 0
        self.clear_calls = 0
        self.clear_result = clear_result

    async def save_async(self, snap: SessionSnapshot) -> None:
        self.saved.append(snap)

    async def clear_async(self) -> bool:
        self.cleared_async_calls += 1
        return self.clear_result

    def clear(self) -> bool:
        self.clear_calls += 1
        return self.clear_result


class FakeHealth:
    def __init__(self):
        self.states: list[str] = []

    def update_vmc_state(self, state: str) -> None:
        self.states.append(state)


class FakeAvailability:
    def __init__(self):
        self.states: list[str] = []

    def set_fsm_state(self, state: str) -> None:
        self.states.append(state)


class FakeDisplay:
    def __init__(self):
        self.states: list[str] = []

    def update_for_state(self, vmc_state: str) -> None:
        self.states.append(vmc_state)


def not_open_snapshot(state: str | None) -> SessionSnapshot:
    return SessionSnapshot(state=state or "idle", credit_escrow=0.0)


def open_snapshot(state: str | None) -> SessionSnapshot:
    return SessionSnapshot(state=state or "dispensing", credit_escrow=2.50)


def make_outputs(
    *,
    tasks: FakeTaskRunner | None = None,
    snapshot=not_open_snapshot,
    credit_escrow=lambda: 0.0,
    selected_product_name=lambda: None,
    pay104_active=lambda: False,
) -> tuple[StatusOutputs, FakeTaskRunner]:
    runner = tasks if tasks is not None else FakeTaskRunner()
    outputs = StatusOutputs(
        snapshot=snapshot,
        credit_escrow=credit_escrow,
        selected_product_name=selected_product_name,
        pay104_active=pay104_active,
        tasks=runner,
    )
    return outputs, runner


# --- state_changed ---


async def test_state_changed_with_no_sinks_is_a_no_op():
    calls: list = []
    outputs, runner = make_outputs(
        snapshot=lambda state: calls.append(state) or not_open_snapshot(state)
    )
    runner.attach(asyncio.get_running_loop())

    outputs.state_changed("idle")
    await asyncio.sleep(0)

    # No session store attached -> persist() returns before ever reading
    # the snapshot callable.
    assert calls == []


async def test_state_changed_with_every_sink_pushes_and_publishes_retained_status():
    outputs, runner = make_outputs(
        credit_escrow=lambda: 3.25,
        selected_product_name=lambda: "Ice Bag",
        snapshot=not_open_snapshot,
    )
    runner.attach(asyncio.get_running_loop())
    mqtt = FakeMqtt()
    health = FakeHealth()
    availability = FakeAvailability()
    store = FakeStore()
    outputs.attach_mqtt(mqtt)
    outputs.attach_health(health)
    outputs.attach_availability(availability)
    outputs.attach_session_store(store)

    outputs.state_changed("interacting_with_user")
    await asyncio.sleep(0)

    assert health.states == ["interacting_with_user"]
    assert availability.states == ["interacting_with_user"]
    assert store.cleared_async_calls == 1  # not-open snapshot -> clear
    assert len(mqtt.published) == 1
    topic, payload, retain = mqtt.published[0]
    assert topic == "status"
    assert retain is True
    assert isinstance(payload, VMCStatus)
    assert payload.state == "interacting_with_user"
    assert payload.credit_escrow == 3.25
    assert payload.selected_product == "Ice Bag"
    assert payload.uptime_seconds >= 0


async def test_state_changed_without_mqtt_client_still_pushes_health_and_availability():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    health = FakeHealth()
    availability = FakeAvailability()
    outputs.attach_health(health)
    outputs.attach_availability(availability)

    outputs.state_changed("idle")
    await asyncio.sleep(0)

    assert health.states == ["idle"]
    assert availability.states == ["idle"]


# --- persist ---


async def test_persist_without_session_store_is_a_no_op():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())

    outputs.persist("idle")
    await asyncio.sleep(0)

    assert runner.persist == []


async def test_persist_skips_while_pay104_is_active():
    store = FakeStore()
    outputs, runner = make_outputs(pay104_active=lambda: True, snapshot=open_snapshot)
    runner.attach(asyncio.get_running_loop())
    outputs.attach_session_store(store)

    outputs.persist("dispensing")
    await asyncio.sleep(0)

    assert store.saved == []
    assert store.cleared_async_calls == 0


async def test_persist_saves_an_open_snapshot():
    store = FakeStore()
    outputs, runner = make_outputs(snapshot=open_snapshot)
    runner.attach(asyncio.get_running_loop())
    outputs.attach_session_store(store)

    outputs.persist("dispensing")
    await asyncio.sleep(0)

    assert len(store.saved) == 1
    assert store.saved[0].credit_escrow == 2.50
    assert store.cleared_async_calls == 0


async def test_persist_clears_a_snapshot_that_is_not_open():
    store = FakeStore()
    outputs, runner = make_outputs(snapshot=not_open_snapshot)
    runner.attach(asyncio.get_running_loop())
    outputs.attach_session_store(store)

    outputs.persist("idle")
    await asyncio.sleep(0)

    assert store.saved == []
    assert store.cleared_async_calls == 1


# --- save_snapshot_async ---


async def test_save_snapshot_async_without_session_store_is_a_no_op():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())

    await outputs.save_snapshot_async(open_snapshot("dispensing"))
    # no store attached -> nothing to assert on but "did not raise"


async def test_save_snapshot_async_skips_while_pay104_is_active():
    store = FakeStore()
    outputs, runner = make_outputs(pay104_active=lambda: True)
    runner.attach(asyncio.get_running_loop())
    outputs.attach_session_store(store)

    await outputs.save_snapshot_async(open_snapshot("dispensing"))

    assert store.saved == []


async def test_save_snapshot_async_saves_when_no_pay104():
    store = FakeStore()
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    outputs.attach_session_store(store)
    snap = open_snapshot("dispensing")

    await outputs.save_snapshot_async(snap)

    assert store.saved == [snap]


# --- clear_session_evidence ---


def test_clear_session_evidence_without_store_returns_true():
    outputs, _runner = make_outputs()

    assert outputs.clear_session_evidence() is True


def test_clear_session_evidence_returns_the_stores_own_answer():
    store = FakeStore(clear_result=False)
    outputs, _runner = make_outputs()
    outputs.attach_session_store(store)

    assert outputs.clear_session_evidence() is False
    assert store.clear_calls == 1


# --- display ---


def test_display_without_controller_is_a_no_op():
    outputs, _runner = make_outputs()

    outputs.display("idle")  # must not raise


def test_display_calls_update_for_state():
    display = FakeDisplay()
    outputs, _runner = make_outputs()
    outputs.attach_display(display)

    outputs.display("dispensing")

    assert display.states == ["dispensing"]


def test_display_controller_property_returns_what_was_attached():
    display = FakeDisplay()
    outputs, _runner = make_outputs()
    assert outputs.display_controller is None

    outputs.attach_display(display)

    assert outputs.display_controller is display


# --- message / refresh / show_qr callbacks ---


def test_message_calls_callback_only_when_set():
    outputs, _runner = make_outputs()
    outputs.message("hello")  # no callback -> must not raise

    received: list[str] = []
    outputs.set_message_callback(received.append)
    outputs.message("hello again")

    assert received == ["hello again"]


def test_refresh_calls_callback_only_when_set():
    outputs, _runner = make_outputs()
    outputs.refresh()  # no callback -> must not raise

    calls = []
    outputs.set_update_callback(lambda: calls.append(True))
    outputs.refresh()

    assert calls == [True]


def test_show_qr_calls_callback_only_when_set():
    outputs, _runner = make_outputs()
    outputs.show_qr(object())  # no callback -> must not raise

    received = []
    outputs.set_qrcode_callback(received.append)
    image = object()
    outputs.show_qr(image)

    assert received == [image]


# --- publish_payment_enable / publish_refund / publish_alert ---


async def test_publish_payment_enable_without_client_is_silent():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())

    outputs.publish_payment_enable(True)
    await asyncio.sleep(0)
    # no client -> nothing to assert but "did not raise"


async def test_publish_payment_enable_publishes_on_the_right_topic():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    mqtt = FakeMqtt()
    outputs.attach_mqtt(mqtt)

    outputs.publish_payment_enable(True)
    await asyncio.sleep(0)

    assert len(mqtt.published) == 1
    topic, payload, _retain = mqtt.published[0]
    assert topic == "cmd/payment/enable"
    assert isinstance(payload, PaymentEnableCommand)
    assert payload.accept is True


async def test_publish_refund_without_client_is_silent():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    cmd = PaymentRefundCommand(request_id="r" * 8, amount=1.50, reason="admin")

    outputs.publish_refund(cmd)
    await asyncio.sleep(0)


async def test_publish_refund_publishes_on_the_right_topic():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    mqtt = FakeMqtt()
    outputs.attach_mqtt(mqtt)
    cmd = PaymentRefundCommand(request_id="r" * 8, amount=1.50, reason="admin")

    outputs.publish_refund(cmd)
    await asyncio.sleep(0)

    assert len(mqtt.published) == 1
    topic, payload, _retain = mqtt.published[0]
    assert topic == "cmd/payment/refund"
    assert payload is cmd


async def test_publish_alert_without_client_is_silent():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    alert = VMCAlert(level=AlertLevel.warning, message="boom", code=None)

    outputs.publish_alert(alert)
    await asyncio.sleep(0)


async def test_publish_alert_publishes_on_the_right_topic():
    outputs, runner = make_outputs()
    runner.attach(asyncio.get_running_loop())
    mqtt = FakeMqtt()
    outputs.attach_mqtt(mqtt)
    alert = VMCAlert(level=AlertLevel.warning, message="boom", code=None)

    outputs.publish_alert(alert)
    await asyncio.sleep(0)

    assert len(mqtt.published) == 1
    topic, payload, _retain = mqtt.published[0]
    assert topic == "alerts"
    assert payload is alert


# --- PaymentGatewayManager.next_payment_prompt ---


def test_next_payment_prompt_returns_none_with_no_gateways():
    manager = PaymentGatewayManager()
    manager.gateways = {}

    assert manager.next_payment_prompt(2.50) is None


def test_next_payment_prompt_cycles_through_gateways():
    manager = PaymentGatewayManager()
    names = list(manager.gateways.keys())
    assert len(names) >= 2  # stripe/paypal/square -- cycling is meaningful

    seen = [manager.next_payment_prompt(2.50)[0] for _ in names]

    assert seen == names
    # Cycled all the way back around to the first gateway.
    assert manager.next_payment_prompt(2.50)[0] == names[0]


def test_next_payment_prompt_returns_a_qr_image():
    manager = PaymentGatewayManager()
    name, qr_image = manager.next_payment_prompt(2.50)

    assert name in manager.gateways
    assert qr_image is not None
