# tests/test_command_dispatcher.py
"""Tests for services/command_dispatcher.py.

Every test drives the dispatcher exactly the way production code does: it
calls ``send()``, and any ack is delivered by invoking the handler the
dispatcher itself registered with the (fake) MQTT client — never by poking
the dispatcher's internal state directly. The fake clock's ``sleep()``
blocks on an ``asyncio.Event`` until the test explicitly fires it, so a
"timeout" is a deterministic, zero-real-time event under the test's control
rather than a race against real wall-clock sleeps.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from contracts.common import CommandAck, SubsystemCommand
from services.command_dispatcher import CommandDispatcher, CommandTimeout


# --- Fakes -------------------------------------------------------------


class FakeClock:
    """Each sleep() call blocks until the test fires it via fire_next()/
    fire_all(). ``calls`` records the requested duration of every sleep()
    invocation, in order — used to prove a broker-down send never calls
    sleep at all (elapsed injected-clock time is zero).
    """

    def __init__(self):
        self.calls: list[float] = []
        self._events: list[asyncio.Event] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        ev = asyncio.Event()
        self._events.append(ev)
        await ev.wait()

    def fire_next(self) -> None:
        """Let the oldest still-pending sleep() call return now (simulates
        that attempt's ack-wait timing out)."""
        ev = self._events.pop(0)
        ev.set()


class FakeMQTTClient:
    """Mimics services/mqtt_client.py's MQTTClient just enough: register()
    records the handler, publish() records the call (dropped silently when
    not connected, like the real client), and deliver_ack() simulates an
    inbound message by calling the registered handler directly — the same
    path a real ack takes through MQTTClient._dispatch.
    """

    def __init__(self, connected: bool = True):
        self.connected = connected
        self.published: list[tuple[str, SubsystemCommand]] = []
        self._handlers: dict[str, callable] = {}

    def register(self, topic_suffix, handler):
        self._handlers[topic_suffix] = handler

    async def publish(self, topic_suffix, payload, qos: int = 1, retain: bool = False):
        if not self.connected:
            return
        self.published.append((topic_suffix, payload))

    async def deliver_ack(self, subsystem: str, payload: dict) -> None:
        handler = self._handlers["cmd/+/ack"]
        await handler(f"cmd/{subsystem}/ack", payload)


def _ack_payload(request_id: str, command: str, status: str = "ok", **extra) -> dict:
    payload = {
        "request_id": request_id,
        "command": command,
        "status": status,
        "detail": None,
        "result": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(extra)
    return payload


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    """Poll predicate() until true — used only to let a background send()
    task reach its publish()/sleep() point before the test proceeds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _wait_for_attempt(clock: FakeClock, n: int) -> None:
    """Wait until the nth clock.sleep() call has actually started (i.e. its
    Event exists in clock._events and can be fired) — not merely until the
    matching publish() has been recorded, which happens one event-loop tick
    earlier and races clock.fire_next() if awaited on its own."""
    await _wait_until(lambda: len(clock.calls) >= n)


# --- Tests ---------------------------------------------------------------


async def test_ack_for_sent_request_id_resolves_send():
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(dispatcher.send("ice_maker", "ping"))
    await _wait_until(lambda: len(mqtt.published) == 1)

    request_id = mqtt.published[0][1].request_id
    await mqtt.deliver_ack("ice_maker", _ack_payload(request_id, "ping"))

    ack = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(ack, CommandAck)
    assert ack.request_id == request_id
    assert ack.status == "ok"
    # No timeout wait was ever satisfied — the ack won the race outright.
    assert clock.calls == [5.0]  # sleep() was scheduled but never fired


async def test_foreign_request_id_is_ignored_and_original_still_times_out():
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, retries=0, clock=clock)

    task = asyncio.ensure_future(dispatcher.send("ice_maker", "ping"))
    await _wait_for_attempt(clock, 1)

    # An ack for a request_id this send() never sent — must be dropped
    # silently, not raise, and must not resolve the real send().
    await mqtt.deliver_ack("ice_maker", _ack_payload("deadbeefdeadbeef", "ping"))
    await asyncio.sleep(0)
    assert not task.done()

    # The genuine attempt still times out (retries=0 -> raises after this).
    clock.fire_next()
    with pytest.raises(CommandTimeout):
        await asyncio.wait_for(task, timeout=2.0)


async def test_timeout_triggers_exactly_one_retry_with_same_request_id():
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, retries=1, clock=clock)

    task = asyncio.ensure_future(dispatcher.send("ice_maker", "ping"))
    await _wait_for_attempt(clock, 1)
    first_request_id = mqtt.published[0][1].request_id

    clock.fire_next()  # first attempt times out
    await _wait_for_attempt(clock, 2)
    second_request_id = mqtt.published[1][1].request_id

    assert len(mqtt.published) == 2  # exactly one retry, not more
    assert second_request_id == first_request_id  # the safety property

    await mqtt.deliver_ack("ice_maker", _ack_payload(first_request_id, "ping"))
    ack = await asyncio.wait_for(task, timeout=2.0)
    assert ack.request_id == first_request_id


async def test_command_timeout_raised_after_retry_also_times_out():
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, retries=1, clock=clock)

    task = asyncio.ensure_future(dispatcher.send("ice_maker", "self_test"))
    await _wait_for_attempt(clock, 1)
    clock.fire_next()  # attempt 1 times out -> retry
    await _wait_for_attempt(clock, 2)
    clock.fire_next()  # attempt 2 (the retry) also times out

    with pytest.raises(CommandTimeout) as excinfo:
        await asyncio.wait_for(task, timeout=2.0)
    assert excinfo.value.subsystem == "ice_maker"
    assert excinfo.value.command == "self_test"
    assert len(mqtt.published) == 2  # no third attempt


async def test_concurrent_sends_to_different_subsystems_do_not_cross_correlate():
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task_a = asyncio.ensure_future(dispatcher.send("ice_maker", "ping"))
    task_b = asyncio.ensure_future(dispatcher.send("vending", "ping"))

    # Both must genuinely be in flight before either is resolved.
    await _wait_until(lambda: len(mqtt.published) == 2)
    assert not task_a.done()
    assert not task_b.done()

    published_by_subsystem = {topic: cmd for topic, cmd in mqtt.published}
    id_a = published_by_subsystem["cmd/ice_maker"].request_id
    id_b = published_by_subsystem["cmd/vending"].request_id
    assert id_a != id_b

    # Deliver B's ack first, deliberately out of send order, to prove
    # correlation is by id, not by call order.
    await mqtt.deliver_ack("vending", _ack_payload(id_b, "ping"))
    await mqtt.deliver_ack("ice_maker", _ack_payload(id_a, "ping"))

    ack_a, ack_b = await asyncio.gather(
        asyncio.wait_for(task_a, timeout=2.0), asyncio.wait_for(task_b, timeout=2.0)
    )
    assert ack_a.request_id == id_a
    assert ack_b.request_id == id_b


@pytest.mark.parametrize("status", ["rejected", "failed", "unsupported"])
async def test_non_ok_ack_resolves_normally(status):
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(dispatcher.send("mdb", "card_reader_test"))
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id

    await mqtt.deliver_ack(
        "mdb", _ack_payload(request_id, "card_reader_test", status=status)
    )
    ack = await asyncio.wait_for(task, timeout=2.0)
    assert ack.status == status  # an answer, not an exception


async def test_broker_down_raises_promptly_without_waiting_out_timeout():
    mqtt = FakeMQTTClient(connected=False)
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    with pytest.raises(CommandTimeout):
        await asyncio.wait_for(dispatcher.send("ice_maker", "ping"), timeout=1.0)

    assert mqtt.published == []  # never even tried to publish
    assert clock.calls == []  # never consulted the timeout clock at all
