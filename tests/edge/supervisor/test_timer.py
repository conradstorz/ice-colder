import asyncio

from ice_edge.supervisor.task_runner import TaskRunner, Timer
from tests.fakes import FakeTaskRunner


def _timer():
    runner = FakeTaskRunner()
    return Timer(runner, "t"), runner


def test_new_timer_is_not_armed():
    timer, runner = _timer()
    assert not timer.armed
    assert runner.scheduled == []


def test_arm_schedules_under_label_and_fires_once():
    timer, runner = _timer()
    hits = []
    timer.arm(5.0, lambda: hits.append(1))
    assert timer.armed
    assert [(c.label, c.delay) for c in runner.scheduled] == [("t", 5.0)]
    runner.fire("t")
    assert hits == [1]
    assert not timer.armed


def test_armed_is_false_inside_the_callback():
    timer, runner = _timer()
    seen = []
    timer.arm(1.0, lambda: seen.append(timer.armed))
    runner.fire("t")
    assert seen == [False]


def test_callback_can_rearm_itself():
    timer, runner = _timer()
    hits = []

    def cb():
        hits.append(1)
        if len(hits) < 2:
            timer.arm(1.0, cb)

    timer.arm(1.0, cb)
    runner.fire("t")
    assert timer.armed
    runner.fire("t")
    assert hits == [1, 1]
    assert not timer.armed


def test_arm_replaces_the_armed_timer():
    timer, runner = _timer()
    hits = []
    timer.arm(1.0, lambda: hits.append("old"))
    timer.arm(2.0, lambda: hits.append("new"))
    assert len(runner.scheduled) == 1
    runner.fire("t")
    assert hits == ["new"]


def test_cancel_disarms_and_is_idempotent():
    timer, runner = _timer()
    timer.cancel()
    timer.arm(1.0, lambda: None)
    timer.cancel()
    timer.cancel()
    assert not timer.armed
    assert runner.scheduled == []


def test_arm_if_idle_arms_only_when_nothing_armed():
    timer, runner = _timer()
    hits = []
    assert timer.arm_if_idle(1.0, lambda: hits.append("first")) is True
    assert timer.arm_if_idle(2.0, lambda: hits.append("second")) is False
    assert [c.delay for c in runner.scheduled] == [1.0]
    runner.fire("t")
    assert hits == ["first"]
    assert timer.arm_if_idle(3.0, lambda: hits.append("third")) is True


def test_externally_cancelled_task_is_not_armed():
    timer, runner = _timer()
    timer.arm(1.0, lambda: None)
    runner.cancel_pending()
    assert not timer.armed
    assert timer.arm_if_idle(1.0, lambda: None) is True


def test_no_loop_leaves_timer_unarmed():
    from ice_edge.supervisor.task_runner import TaskRunner

    timer = Timer(TaskRunner(), "t")
    timer.arm(1.0, lambda: None)
    assert not timer.armed


async def test_cancel_pending_then_arm_if_idle_arms_a_replacement_at_once():
    runner = TaskRunner()
    runner.attach(asyncio.get_running_loop())
    timer = Timer(runner, "t")
    hits = []
    timer.arm(60.0, lambda: hits.append("old"))
    assert timer.armed

    runner.cancel_pending()
    # No await: the cancelled task has not yet been processed by the loop.
    assert not timer.armed
    assert timer.arm_if_idle(0.01, lambda: hits.append("new")) is True
    assert timer.armed

    await asyncio.sleep(0.1)
    assert hits == ["new"]
