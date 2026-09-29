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
from services.command_dispatcher import (
    CommandDispatcher,
    CommandTimeout,
    CompletionTimeout,
)


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

    deliver_dispenser_report() is the completion-table amendment's second
    entry point: CommandDispatcher.__init__ now also registers a handler
    for `hardware/dispenser` (dispense's completion signal, distinct from
    the ack channel `deliver_ack` drives) — this simulates the vending
    simulator's terminal report the same way deliver_ack simulates an ack.
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

    async def deliver_dispenser_report(self, payload: dict) -> None:
        handler = self._handlers["hardware/dispenser"]
        await handler("hardware/dispenser", payload)


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


# --- send_and_await_completion (2026-09-29 completion-table amendment) ----
#
# Every test below drives send_and_await_completion the same way the tests
# above drive send(): only by publishing/delivering messages through the
# fake MQTT client and the injected clock, never by poking dispatcher
# internals directly (except test_early_completion_signal_is_not_lost,
# which deliberately reproduces the documented registration-gap race by
# controlling delivery order).


async def test_immediate_command_matches_send_exactly():
    """ping (no COMPLETION_TIMEOUTS entry) must behave EXACTLY like send()
    -- no second wait, no second clock.sleep call. This is the "an
    immediate command is unaffected" proof: ping's contract with the
    dispatcher does not change at all."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("ice_maker", "ping")
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id

    await mqtt.deliver_ack("ice_maker", _ack_payload(request_id, "ping"))
    ack = await asyncio.wait_for(task, timeout=2.0)

    assert ack.request_id == request_id
    assert ack.status == "ok"
    assert ack.phase == "completed"
    # Only the ack-phase sleep was ever scheduled -- no second, completion
    # wait was started for an immediate command.
    assert clock.calls == [5.0]


async def test_dispense_completion_awaits_terminal_hardware_dispenser_report():
    """dispense's completion signal is NOT a second ack -- it is the
    terminal `hardware/dispenser` report, correlated by request_id
    (contracts/common.py COMPLETION_TIMEOUTS, the completion-table
    amendment). The accept ack alone must not resolve
    send_and_await_completion."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "dispense", {"slot": 3})
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id

    await mqtt.deliver_ack(
        "vending",
        _ack_payload(
            request_id, "dispense", status="ok", result={"slot": 3}, phase="accepted"
        ),
    )
    # Let send_and_await_completion actually resume from send() and
    # register its completion-wait Future -- a single sleep(0) is not
    # reliably enough turns of the event loop for that whole chain
    # (future.set_result -> task resumption -> send() return -> phase
    # check -> completion-timeout lookup -> Future registration) to
    # finish; polling for the registration itself is what's actually true,
    # not a guess at how many ticks it takes.
    await _wait_until(lambda: request_id in dispatcher._pending_completions)

    assert not task.done()  # the accept ack alone must not resolve this

    await mqtt.deliver_dispenser_report(
        {
            "slot": 3,
            "state": "complete",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    ack = await asyncio.wait_for(task, timeout=2.0)

    assert ack.request_id == request_id
    assert ack.command == "dispense"
    assert ack.status == "ok"
    assert ack.phase == "completed"
    # The completion wait used dispense's own 120s timeout, not the 5s ack
    # timeout -- two distinct, separately-consulted timeouts.
    assert clock.calls == [5.0, 120.0]


async def test_dispense_non_complete_terminal_state_resolves_as_failed():
    """A terminal outcome other than `complete` (bin_empty, timeout, jam,
    error) is still a completion -- the run finished, just not
    successfully -- and must resolve send_and_await_completion with a
    "failed" status, not leave it hanging until the completion timeout."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "dispense", {"slot": 1})
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id
    await mqtt.deliver_ack(
        "vending", _ack_payload(request_id, "dispense", status="ok", phase="accepted")
    )
    await asyncio.sleep(0)

    await mqtt.deliver_dispenser_report(
        {
            "slot": 1,
            "state": "jam",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    ack = await asyncio.wait_for(task, timeout=2.0)

    assert ack.status == "failed"
    assert ack.detail == "jam"


async def test_water_valve_completion_awaits_second_completed_ack():
    """water_valve's completion signal IS a second ack on the same topic
    and request_id, phase="completed" -- the choice documented in
    simulators/vending_machine.py's _handle_water_valve. The timeout is
    derived from `seconds`, not a fixed constant."""
    from contracts.common import COMPLETION_TIMEOUTS

    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "water_valve", {"seconds": 3})
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id

    await mqtt.deliver_ack(
        "vending",
        _ack_payload(request_id, "water_valve", status="ok", phase="accepted"),
    )
    # See test_dispense_completion_awaits_terminal_hardware_dispenser_report's
    # comment: poll for the actual registration rather than guessing a
    # sleep(0) count.
    await _wait_until(lambda: request_id in dispatcher._pending_completions)
    assert not task.done()

    await mqtt.deliver_ack(
        "vending",
        _ack_payload(
            request_id,
            "water_valve",
            status="ok",
            result={"seconds": 3},
            phase="completed",
        ),
    )
    ack = await asyncio.wait_for(task, timeout=2.0)

    assert ack.phase == "completed"
    assert ack.result == {"seconds": 3}
    expected_completion_timeout = COMPLETION_TIMEOUTS[("vending", "water_valve")](
        {"seconds": 3}
    )
    assert clock.calls == [5.0, expected_completion_timeout]


async def test_power_cycle_completion_timeout_scales_with_dwell_seconds():
    """The trap called out in the design: power_cycle's dwell_seconds can
    legitimately be 300s. A fixed completion timeout would spuriously fail
    that; this proves the actual wait derives from the parameter."""
    from contracts.common import COMPLETION_TIMEOUTS

    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion(
            "ice_maker", "power_cycle", {"dwell_seconds": 300}
        )
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id
    await mqtt.deliver_ack(
        "ice_maker",
        _ack_payload(request_id, "power_cycle", status="ok", phase="accepted"),
    )
    await _wait_until(lambda: request_id in dispatcher._pending_completions)

    await mqtt.deliver_ack(
        "ice_maker",
        _ack_payload(
            request_id,
            "power_cycle",
            status="ok",
            result={"dwell_seconds": 300},
            phase="completed",
        ),
    )
    ack = await asyncio.wait_for(task, timeout=2.0)

    assert ack.phase == "completed"
    expected_completion_timeout = COMPLETION_TIMEOUTS[("ice_maker", "power_cycle")](
        {"dwell_seconds": 300}
    )
    assert expected_completion_timeout == 330.0  # 300 + the 30s margin
    assert clock.calls == [5.0, expected_completion_timeout]


async def test_ack_timeout_still_fires_for_long_running_command_that_never_accepts():
    """THE regression this design most easily introduces (proof standard):
    a subsystem that never even acks a long-running command must still
    fail in ~ack_timeout per attempt via CommandTimeout -- never wait out
    the much longer completion timeout. Proved by never delivering
    anything at all and firing only the ack-phase clock events."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, retries=1, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "dispense", {"slot": 0})
    )
    await _wait_for_attempt(clock, 1)
    clock.fire_next()  # attempt 1 (ack) times out -> retry
    await _wait_for_attempt(clock, 2)
    clock.fire_next()  # attempt 2 (the retry) also times out

    with pytest.raises(CommandTimeout):
        await asyncio.wait_for(task, timeout=2.0)

    # Exactly two ack-phase sleeps (5.0 each) -- the 120s completion
    # timeout was never even consulted, because the command was never
    # accepted in the first place.
    assert clock.calls == [5.0, 5.0]


async def test_completion_timeout_raised_when_accepted_but_never_completes():
    """The command IS accepted (the ack round trip succeeds) but nothing
    ever reports completion -- CompletionTimeout, not CommandTimeout, and
    only after the command's own (much longer) completion timeout, not the
    ack timeout."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "dispense", {"slot": 0})
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id
    await mqtt.deliver_ack(
        "vending", _ack_payload(request_id, "dispense", status="ok", phase="accepted")
    )

    # Wait for the completion-phase sleep (120s) to actually be scheduled,
    # then fire it -- nothing ever delivers a completion signal. The
    # ack-phase sleep's own event is still sitting unpopped in the fake
    # clock's queue (it lost the race to the delivered ack and was
    # cancelled, not fired -- FakeClock never removes an unfired event on
    # cancellation), so firing the completion event is the SECOND
    # fire_next() call, not the first.
    await _wait_for_attempt(clock, 2)
    assert clock.calls == [5.0, 120.0]
    clock.fire_next()  # the stale, already-cancelled ack-phase event
    clock.fire_next()  # the real completion-phase event

    with pytest.raises(CompletionTimeout) as excinfo:
        await asyncio.wait_for(task, timeout=2.0)
    assert excinfo.value.subsystem == "vending"
    assert excinfo.value.command == "dispense"


async def test_early_completion_signal_is_not_lost():
    """Reproduces the registration-gap race documented in
    CommandDispatcher._resolve_completion: both the accept ack and the
    completion report are delivered back-to-back, before
    send_and_await_completion's own coroutine has had a chance to resume
    from send() and register its completion Future. Without the
    _early_completions cache this hangs until the (unfired) 120s clock
    event, and asyncio.wait_for below would raise asyncio.TimeoutError
    instead of returning."""
    mqtt = FakeMQTTClient()
    clock = FakeClock()
    dispatcher = CommandDispatcher(mqtt, timeout=5.0, clock=clock)

    task = asyncio.ensure_future(
        dispatcher.send_and_await_completion("vending", "dispense", {"slot": 0})
    )
    await _wait_until(lambda: len(mqtt.published) == 1)
    request_id = mqtt.published[0][1].request_id

    # Both delivered here, synchronously, before anything yields back to
    # `task` -- so send_and_await_completion has not yet resumed from
    # send() when the completion report arrives.
    await mqtt.deliver_ack(
        "vending", _ack_payload(request_id, "dispense", status="ok", phase="accepted")
    )
    await mqtt.deliver_dispenser_report(
        {
            "slot": 0,
            "state": "complete",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    ack = await asyncio.wait_for(task, timeout=2.0)
    assert ack.status == "ok"
    assert ack.phase == "completed"
    assert request_id not in dispatcher._early_completions  # consumed, not leaked
