from ice_edge.sale.session_timer import SessionTimer
from tests.fakes import FakeTaskRunner


def _timer(seconds=None):
    runner = FakeTaskRunner()
    box = {"s": 30.0} if seconds is None else seconds
    fired = []
    timer = SessionTimer(runner, lambda: box["s"], lambda: fired.append(1))
    return timer, runner, box, fired


def test_arm_schedules_labelled_call_with_current_seconds():
    timer, runner, _, _ = _timer()
    timer.arm()
    assert [(c.label, c.delay) for c in runner.scheduled] == [("session_timeout", 30.0)]
    assert timer.armed


def test_arm_twice_cancels_first():
    timer, runner, _, _ = _timer()
    timer.arm()
    timer.arm()
    assert runner.calls[0].task.cancelled
    assert len(runner.scheduled) == 1


def test_cancel_disarms_and_is_noop_when_idle():
    timer, runner, _, _ = _timer()
    timer.cancel()
    assert not timer.armed
    timer.arm()
    timer.cancel()
    assert not timer.armed
    assert runner.calls[0].task.cancelled
    assert runner.scheduled == []


def test_fire_calls_on_expire_once():
    timer, runner, _, fired = _timer()
    timer.arm()
    runner.fire("session_timeout")
    assert fired == [1]


def test_seconds_read_at_arm_time():
    timer, runner, box, _ = _timer()
    timer.arm()
    box["s"] = 5.0
    timer.arm()
    assert runner.calls[1].delay == 5.0
