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
    fsm_state="idle",
    escrow_is_empty=True,
    make_idle_for_service=None,
):
    """`fsm_state`/`escrow_is_empty`/`make_idle_for_service` (Task 11,
    lease-preconditions) back `begin_maintenance`/`begin_standby`'s own
    FSM checks -- every pre-Task-11 caller here never touches those two
    methods, so the defaults ("idle", True, a no-op returning True) are
    inert for them. `make_idle_for_service` defaults to `lambda: True`
    when not given; pass a recording callable to assert on when/whether
    it was actually called.
    """
    scheduler = FakeScheduler()
    granted: list[None] = []
    released: list[str] = []
    idle_for_service = (
        make_idle_for_service if make_idle_for_service is not None else (lambda: True)
    )
    lease = MaintenanceLease(
        schedule=scheduler,
        on_granted=lambda: granted.append(None),
        on_released=lambda by: released.append(by),
        idle_timeout=lambda: idle_timeout,
        takeover_idle=lambda: takeover_idle,
        sweep_seconds=lambda: sweep_seconds,
        fsm_state=lambda: fsm_state,
        escrow_is_empty=lambda: escrow_is_empty,
        make_idle_for_service=idle_for_service,
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


# --- begin_maintenance / begin_standby / end_maintenance /
#     take_over_maintenance (Task 11, lease-preconditions) ------------------
#
# These four moved onto MaintenanceLease verbatim from VMC; the FSM reads
# that used to be `self.state`/`self._escrow.is_empty_within_tolerance`
# are now the `fsm_state`/`escrow_is_empty` callables `make_lease` wires in
# above, and `begin_standby`'s own per-state refund/cancel block is now a
# single call to `make_idle_for_service` (recorded below via a custom
# callable rather than the make_lease default).


def test_begin_maintenance_refused_when_fsm_not_idle():
    lease, *_ = make_lease(fsm_state="interacting_with_user")
    granted, reason = lease.begin_maintenance("user-1", "sess-1")
    assert granted is False
    assert reason == "machine is mid-sale"
    assert lease.hold is None


def test_begin_maintenance_refused_when_escrow_nonzero_even_while_idle():
    lease, *_ = make_lease(fsm_state="idle", escrow_is_empty=False)
    granted, reason = lease.begin_maintenance("user-1", "sess-1")
    assert granted is False
    assert reason == "credit is still on the machine"
    assert lease.hold is None


def test_begin_maintenance_refused_when_lease_exists_names_holder():
    lease, *_ = make_lease()
    lease.grant("owner-1", "sess-a", standby=False)

    granted, reason = lease.begin_maintenance("tech-2", "sess-b")

    assert granted is False
    assert reason == "held by owner-1"
    assert lease.hold.holder_user_id == "owner-1"


def test_begin_maintenance_grants_when_idle_with_zero_escrow():
    lease, *_ = make_lease(fsm_state="idle", escrow_is_empty=True)
    granted, reason = lease.begin_maintenance("user-1", "sess-1")
    assert granted is True
    assert reason is None
    assert lease.hold is not None
    assert lease.hold.holder_user_id == "user-1"
    assert lease.hold.standby is False


def test_begin_standby_refused_while_dispensing():
    calls: list[None] = []
    lease, *_ = make_lease(
        fsm_state="dispensing", make_idle_for_service=lambda: calls.append(None)
    )
    granted, reason = lease.begin_standby("user-1", "sess-1")
    assert granted is False
    assert reason == "vend finishing, tap again"
    assert lease.hold is None
    assert calls == []  # make_idle_for_service never called while dispensing


def test_begin_standby_refused_when_held_by_a_different_session():
    calls: list[None] = []
    lease, *_ = make_lease(make_idle_for_service=lambda: calls.append(None) or True)
    lease.grant("owner-1", "sess-a", standby=False)

    granted, reason = lease.begin_standby("tech-2", "sess-b")

    assert granted is False
    assert reason == "held by owner-1"
    assert calls == []  # the held-by-another-session path never calls it


def test_begin_standby_upgrades_in_place_for_holders_own_session():
    calls: list[None] = []
    lease, *_ = make_lease(make_idle_for_service=lambda: calls.append(None) or True)
    lease.grant("user-1", "sess-1", standby=False)
    hold_before = lease.hold

    granted, reason = lease.begin_standby("user-1", "sess-1")

    assert granted is True
    assert reason is None
    assert lease.hold is hold_before  # same object, upgraded in place
    assert lease.hold.standby is True
    assert calls == []  # the upgrade-in-place path never calls it either


def test_begin_standby_fresh_grant_calls_make_idle_for_service_then_grants():
    calls: list[None] = []
    lease, scheduler, granted_log, released = make_lease(
        fsm_state="idle", make_idle_for_service=lambda: calls.append(None) or True
    )

    granted, reason = lease.begin_standby("user-1", "sess-1")

    assert granted is True
    assert reason is None
    assert calls == [None]  # called exactly once, on the fresh-grant path
    assert lease.hold is not None
    assert lease.hold.standby is True
    assert lease.hold.holder_user_id == "user-1"


def test_end_maintenance_delegates_to_request_release():
    lease, *_ = make_lease()
    lease.grant("user-1", "sess-1", standby=False)

    assert lease.end_maintenance("sess-other") is False
    assert lease.hold is not None

    assert lease.end_maintenance("sess-1") is True
    assert lease.hold is None


def test_take_over_maintenance_delegates_to_take_over():
    lease, *_ = make_lease(takeover_idle=60.0)
    lease.grant("user-1", "sess-1", standby=False)

    refused, reason = lease.take_over_maintenance("user-2", "sess-2")
    assert refused is False
    assert reason == "lease not yet idle"

    lease.hold.last_activity_at = time.time() - 61.0
    granted, reason = lease.take_over_maintenance("user-2", "sess-2")
    assert granted is True
    assert reason is None
    assert lease.hold.holder_user_id == "user-2"
