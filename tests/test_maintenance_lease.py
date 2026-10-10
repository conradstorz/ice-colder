"""Unit tests for controller.maintenance_lease.MaintenanceLease -- the lease
lifecycle extracted from VMC (sixth and last cut at the VMC god object; see
CLAUDE.md's "FSM Core" section). These exercise the lease in isolation, with
no VMC and no asyncio event loop: ``schedule`` is a fake that records
``(delay, callback)`` and hands back a dummy task, so timers are advanced by
calling the recorded callback directly rather than waiting on real time.
"""

import time

import pytest
from loguru import logger

from controller.maintenance_lease import MaintenanceHold, MaintenanceLease


@pytest.fixture
def loud_log():
    """Capture loguru output at INFO+ so a test can assert on log wording
    (mirrors the fixture of the same name in tests/test_maintenance_standby.py)."""
    records: list[str] = []
    handle = logger.add(
        lambda m: records.append(m.record["message"]),
        level="INFO",
        format="{message}",
    )
    try:
        yield records
    finally:
        logger.remove(handle)


class FakeTask:
    def __init__(self):
        self._done = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self._done = True


class FakeScheduler:
    """Records every (delay, callback) scheduled; never fires on its own."""

    def __init__(self):
        self.calls: list[tuple[float, object]] = []

    def __call__(self, delay, callback, *, label=""):
        self.calls.append((delay, callback))
        return FakeTask()

    @property
    def last_callback(self):
        return self.calls[-1][1]

    @property
    def last_delay(self):
        return self.calls[-1][0]


def make_lease(
    *,
    idle_timeout=5.0,
    takeover_idle=2.0,
    sweep_seconds=3.0,
):
    scheduler = FakeScheduler()
    granted: list[None] = []
    released: list[str] = []
    lease = MaintenanceLease(
        schedule=scheduler,
        on_granted=lambda: granted.append(None),
        on_released=lambda by: released.append(by),
        idle_timeout=lambda: idle_timeout,
        takeover_idle=lambda: takeover_idle,
        sweep_seconds=lambda: sweep_seconds,
    )
    return lease, scheduler, granted, released


# --- grant ---


def test_grant_opportunistic_arms_idle_timer_and_calls_on_granted():
    lease, scheduler, granted, released = make_lease(idle_timeout=42.0)

    lease.grant("user-1", "sess-1", standby=False)

    assert granted == [None]
    assert released == []
    assert lease.hold is not None
    assert lease.hold.holder_user_id == "user-1"
    assert lease.hold.holder_session_id == "sess-1"
    assert lease.hold.standby is False
    assert lease.idle_task is not None
    assert lease.sweep_task is None
    assert scheduler.last_delay == 42.0
    assert scheduler.last_callback == lease.idle_expired


def test_grant_standby_with_predicate_arms_sweep():
    lease, scheduler, granted, released = make_lease(sweep_seconds=17.0)
    lease.set_session_liveness(lambda session_id: True)

    lease.grant("user-1", "sess-1", standby=True)

    assert granted == [None]
    assert lease.hold.standby is True
    assert lease.sweep_task is not None
    assert lease.idle_task is None
    assert scheduler.last_delay == 17.0
    assert scheduler.last_callback == lease.sweep_tick


def test_grant_standby_with_no_predicate_falls_back_and_warns_once(loud_log):
    lease, scheduler, granted, released = make_lease(idle_timeout=99.0)

    lease.grant("user-1", "sess-1", standby=True)
    assert lease.idle_task is not None
    assert lease.sweep_task is None
    assert scheduler.last_delay == 99.0

    lease.release(by="admin")
    lease.grant("user-2", "sess-2", standby=True)

    warnings = [m for m in loud_log if "no session-liveness predicate" in m]
    assert len(warnings) == 1


# --- request_release ---


def test_request_release_from_non_holder_is_false():
    lease, *_ = make_lease()
    lease.grant("user-1", "sess-1", standby=False)

    assert lease.request_release("sess-other") is False
    assert lease.hold is not None


def test_request_release_from_holder_with_no_runs_releases_immediately():
    lease, scheduler, granted, released = make_lease()
    lease.grant("user-1", "sess-1", standby=False)

    result = lease.request_release("sess-1")

    assert result is True
    assert lease.hold is None
    assert released == ["admin"]


def test_request_release_with_run_in_flight_defers_then_run_finished_releases():
    lease, scheduler, granted, released = make_lease()
    lease.grant("user-1", "sess-1", standby=False)
    lease.run_started()

    result = lease.request_release("sess-1")

    assert result is True
    assert lease.hold is not None
    assert lease.hold.release_requested is True
    assert lease.hold.release_reason == "admin"
    assert released == []

    lease.run_finished()

    assert lease.hold is None
    assert released == ["admin"]


# --- idle_expired ---


def test_idle_expired_with_run_in_flight_defers_with_idle_timeout_reason():
    lease, scheduler, granted, released = make_lease()
    lease.grant("user-1", "sess-1", standby=False)
    lease.run_started()

    lease.idle_expired()

    assert lease.hold is not None
    assert lease.hold.release_requested is True
    assert lease.hold.release_reason == "idle_timeout"
    assert released == []

    lease.run_finished()

    assert lease.hold is None
    assert released == ["idle_timeout"]


def test_idle_expired_on_standby_with_predicate_is_a_noop():
    lease, *_ = make_lease()
    lease.set_session_liveness(lambda session_id: True)
    lease.grant("user-1", "sess-1", standby=True)

    lease.idle_expired()

    assert lease.hold is not None


def test_idle_expired_with_no_hold_is_a_noop():
    lease, *_ = make_lease()
    lease.idle_expired()
    assert lease.hold is None


# --- sweep_tick ---


def test_sweep_tick_rearms_while_holder_session_is_live():
    lease, scheduler, granted, released = make_lease(sweep_seconds=11.0)
    lease.set_session_liveness(lambda session_id: True)
    lease.grant("user-1", "sess-1", standby=True)
    calls_before = len(scheduler.calls)

    lease.sweep_tick()

    assert lease.hold is not None
    assert len(scheduler.calls) == calls_before + 1
    assert scheduler.last_delay == 11.0
    assert scheduler.last_callback == lease.sweep_tick


def test_sweep_tick_defers_with_session_ended_when_dead_and_run_in_flight():
    lease, scheduler, granted, released = make_lease()
    lease.set_session_liveness(lambda session_id: False)
    lease.grant("user-1", "sess-1", standby=True)
    lease.run_started()

    lease.sweep_tick()

    assert lease.hold is not None
    assert lease.hold.release_requested is True
    assert lease.hold.release_reason == "session_ended"
    assert released == []

    lease.run_finished()

    assert lease.hold is None
    assert released == ["session_ended"]


def test_sweep_tick_releases_immediately_when_dead_and_no_runs():
    lease, scheduler, granted, released = make_lease()
    lease.set_session_liveness(lambda session_id: False)
    lease.grant("user-1", "sess-1", standby=True)

    lease.sweep_tick()

    assert lease.hold is None
    assert released == ["session_ended"]


# --- take_over ---


def test_take_over_refuses_with_no_lease_held():
    lease, *_ = make_lease()
    granted, reason = lease.take_over("user-2", "sess-2")
    assert granted is False
    assert reason == "no lease held"


def test_take_over_refuses_while_a_run_is_in_flight():
    lease, *_ = make_lease()
    lease.grant("user-1", "sess-1", standby=False)
    lease.run_started()

    granted, reason = lease.take_over("user-2", "sess-2")

    assert granted is False
    assert reason == "a test is in flight"


def test_take_over_refuses_before_idle_threshold_elapsed():
    lease, *_ = make_lease(takeover_idle=60.0)
    lease.grant("user-1", "sess-1", standby=False)

    granted, reason = lease.take_over("user-2", "sess-2")

    assert granted is False
    assert reason == "lease not yet idle"


def test_take_over_succeeds_after_idle_threshold_and_rearms_by_standby():
    lease, scheduler, granted, released = make_lease(
        takeover_idle=60.0, sweep_seconds=5.0
    )
    lease.set_session_liveness(lambda session_id: True)
    lease.grant("user-1", "sess-1", standby=True)
    lease.hold.last_activity_at = time.time() - 61.0

    ok, reason = lease.take_over("user-2", "sess-2")

    assert ok is True
    assert reason is None
    assert lease.hold.holder_user_id == "user-2"
    assert lease.hold.holder_session_id == "sess-2"
    assert lease.hold.standby is True
    assert lease.hold.release_requested is False
    assert lease.hold.release_reason is None
    assert scheduler.last_callback == lease.sweep_tick


def test_take_over_rearms_idle_timer_for_opportunistic_lease():
    lease, scheduler, granted, released = make_lease(
        takeover_idle=60.0, idle_timeout=7.0
    )
    lease.grant("user-1", "sess-1", standby=False)
    lease.hold.last_activity_at = time.time() - 61.0

    ok, reason = lease.take_over("user-2", "sess-2")

    assert ok is True
    assert lease.hold.standby is False
    assert scheduler.last_callback == lease.idle_expired
    assert scheduler.last_delay == 7.0


# --- run_started / run_finished / test_run ---


def test_run_started_with_no_hold_raises():
    lease, *_ = make_lease()
    with pytest.raises(RuntimeError):
        lease.run_started()


def test_test_run_context_manager_decrements_in_finally_even_on_raise():
    lease, *_ = make_lease()
    lease.grant("user-1", "sess-1", standby=False)

    with pytest.raises(ValueError):
        with lease.test_run():
            assert lease.hold.runs_in_flight == 1
            raise ValueError("boom")

    assert lease.hold is not None
    assert lease.hold.runs_in_flight == 0


def test_hold_dataclass_defaults():
    hold = MaintenanceHold(
        holder_user_id="u",
        holder_session_id="s",
        started_at=1.0,
        last_activity_at=1.0,
    )
    assert hold.runs_in_flight == 0
    assert hold.release_requested is False
    assert hold.release_reason is None
    assert hold.standby is False
