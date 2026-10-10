# tests/test_refund_protocol.py
"""Unit tests for `controller.refund_protocol.RefundProtocol`, in isolation
from `VMC` -- no FSM, no MQTT, no event recorder. `publish` and `schedule`
are fake callables that record what they were given; the schedule fake
captures its callback so a test can fire a deadline by hand, and returns a
dummy "task" object with `.done()`/`.cancel()` so `cancel_all()` and the
deadline-cancel-on-confirm/retry paths have something real to call.
"""

from contracts.vending_machine import PaymentRefundResult, RefundStatus
from controller.refund_protocol import RefundProtocol


class FakeTask:
    def __init__(self):
        self.cancelled = False
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True
        self._done = True


class FakeClock:
    """Records every publish() and schedule() call; lets a test fire a
    captured deadline callback by hand."""

    def __init__(self):
        self.published: list = []
        self.scheduled: list[tuple[float, object]] = []
        self.tasks: list[FakeTask] = []

    def publish(self, cmd) -> None:
        self.published.append(cmd)

    def schedule(self, delay, callback, *, label=""):
        self.scheduled.append((delay, callback))
        task = FakeTask()
        self.tasks.append(task)
        return task


def make_protocol(clock: FakeClock, *, ack_timeout=10.0, max_attempts=2):
    confirmed: list = []
    failed: list = []
    state = {"ack_timeout": ack_timeout, "max_attempts": max_attempts}

    protocol = RefundProtocol(
        publish=clock.publish,
        schedule=clock.schedule,
        on_confirmed=lambda pending, amount: confirmed.append((pending, amount)),
        on_failed=lambda pending, detail: failed.append((pending, detail)),
        ack_timeout=lambda: state["ack_timeout"],
        max_attempts=lambda: state["max_attempts"],
    )
    return protocol, confirmed, failed, state


def ok_ack(request_id: str, amount_returned: float) -> PaymentRefundResult:
    return PaymentRefundResult(
        request_id=request_id, status=RefundStatus.ok, amount_returned=amount_returned
    )


def failed_ack(request_id: str, detail: str) -> PaymentRefundResult:
    return PaymentRefundResult(
        request_id=request_id, status=RefundStatus.failed, detail=detail
    )


# --- begin ---


def test_begin_publishes_one_command_and_arms_deadline():
    clock = FakeClock()
    protocol, _, _, _ = make_protocol(clock, ack_timeout=5.0)

    pending = protocol.begin(12.50, "admin")

    assert len(clock.published) == 1
    cmd = clock.published[0]
    assert cmd.request_id == pending.request_id
    assert cmd.amount == 12.50
    assert cmd.reason == "admin"
    assert len(clock.scheduled) == 1
    delay, _callback = clock.scheduled[0]
    assert delay == 5.0
    assert pending.deadline_task is clock.tasks[0]
    assert pending.request_id in protocol.pending


# --- handle_ack: confirmed ---


def test_ok_ack_confirms_pops_and_cancels_deadline():
    clock = FakeClock()
    protocol, confirmed, failed, _ = make_protocol(clock)
    pending = protocol.begin(5.00, "admin")
    deadline_task = pending.deadline_task

    protocol.handle_ack(ok_ack(pending.request_id, 5.00))

    assert confirmed == [(pending, 5.00)]
    assert failed == []
    assert pending.request_id not in protocol.pending
    assert deadline_task.cancelled is True


# --- handle_ack: retry then terminal failure ---


def test_failed_ack_below_max_attempts_retries_with_same_request_id():
    clock = FakeClock()
    protocol, confirmed, failed, _ = make_protocol(clock, max_attempts=2)
    pending = protocol.begin(5.00, "admin")
    first_request_id = pending.request_id

    protocol.handle_ack(failed_ack(pending.request_id, "changer_empty"))

    assert confirmed == []
    assert failed == []
    assert pending.attempts == 2
    assert pending.request_id == first_request_id
    # Re-sent: a second publish/schedule, both keyed on the SAME request_id.
    assert len(clock.published) == 2
    assert clock.published[1].request_id == first_request_id
    assert len(clock.scheduled) == 2
    assert pending.request_id in protocol.pending


def test_second_failure_at_max_attempts_reaches_on_failed_and_pops():
    clock = FakeClock()
    protocol, confirmed, failed, _ = make_protocol(clock, max_attempts=2)
    pending = protocol.begin(5.00, "admin")

    protocol.handle_ack(failed_ack(pending.request_id, "changer_empty"))
    protocol.handle_ack(failed_ack(pending.request_id, "changer_empty"))

    assert confirmed == []
    assert len(failed) == 1
    failed_pending, detail = failed[0]
    assert failed_pending is pending
    assert detail == "changer_empty"
    assert pending.request_id not in protocol.pending


# --- deadline firing ---


def test_firing_the_deadline_callback_behaves_as_ack_timeout():
    clock = FakeClock()
    protocol, confirmed, failed, _ = make_protocol(clock, max_attempts=1)
    pending = protocol.begin(5.00, "admin")
    _delay, callback = clock.scheduled[0]

    callback()

    assert confirmed == []
    assert len(failed) == 1
    failed_pending, detail = failed[0]
    assert failed_pending is pending
    assert detail == "ack_timeout"
    assert pending.request_id not in protocol.pending


# --- unknown request_id ---


def test_ack_for_unknown_request_id_is_ignored():
    clock = FakeClock()
    protocol, confirmed, failed, _ = make_protocol(clock)
    protocol.begin(5.00, "admin")

    protocol.handle_ack(ok_ack("not-a-real-request-id", 5.00))

    assert confirmed == []
    assert failed == []


# --- cancel_all ---


def test_cancel_all_cancels_every_live_deadline_task():
    clock = FakeClock()
    protocol, _, _, _ = make_protocol(clock)
    first = protocol.begin(5.00, "admin")
    second = protocol.begin(2.00, "admin")

    protocol.cancel_all()

    assert first.deadline_task.cancelled is True
    assert second.deadline_task.cancelled is True


# --- first_request_id ---


def test_first_request_id_is_none_when_empty():
    clock = FakeClock()
    protocol, _, _, _ = make_protocol(clock)

    assert protocol.first_request_id() is None


def test_first_request_id_is_the_earliest_request():
    clock = FakeClock()
    protocol, _, _, _ = make_protocol(clock)
    first = protocol.begin(5.00, "admin")
    protocol.begin(2.00, "admin")

    assert protocol.first_request_id() == first.request_id


# --- ack_timeout/max_attempts read at send time ---


def test_ack_timeout_is_read_fresh_on_each_send():
    clock = FakeClock()
    protocol, _, _, state = make_protocol(clock, ack_timeout=5.0)
    pending = protocol.begin(5.00, "admin")
    assert clock.scheduled[0][0] == 5.0

    state["ack_timeout"] = 0.01
    protocol.attempt_failed(pending, detail="ack_timeout")

    assert clock.scheduled[1][0] == 0.01


def test_max_attempts_is_read_fresh_on_each_failure():
    clock = FakeClock()
    protocol, confirmed, failed, state = make_protocol(clock, max_attempts=1)
    pending = protocol.begin(5.00, "admin")

    # max_attempts raised between the first attempt and its failure: the
    # protocol should see the NEW value and retry instead of giving up.
    state["max_attempts"] = 2
    protocol.handle_ack(failed_ack(pending.request_id, "changer_empty"))

    assert failed == []
    assert pending.attempts == 2
    assert pending.request_id in protocol.pending
