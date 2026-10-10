"""Unit tests for the standby maintenance lease (system-tests design §2.2a).

Standby is VMC.begin_standby: unlike the opportunistic lease
(VMC.begin_maintenance), it makes a busy machine idle itself -- refunding
any escrow and cancelling a live customer session -- rather than refusing
until the machine happens to be idle. It is bound to the holder's web
session via a liveness-predicate sweep instead of the ordinary idle timer.

Reuses the fixture pattern from tests/test_vmc_flows.py's TestMaintenanceLease.
"""

import asyncio

import pytest
from loguru import logger

from tests.fakes import FakeTaskRunner
from tests.test_vmc_flows import (
    RecordingClient,
    _profiles_tmp_base_dir,  # noqa: F401 -- pytest picks this up as an autouse fixture
    make_machine2,
)


@pytest.fixture
def loud_log():
    """Capture loguru output at INFO+ so a test can assert on log wording."""
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


def _active_fault_codes(vmc) -> list[str]:
    return [f["code"] for f in vmc.active_faults()]


async def test_standby_from_interacting_with_user_refunds_and_idles():
    machine = make_machine2()
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 1.00

    granted, reason = machine.lease.begin_standby("user-1", "sess-1")
    await asyncio.sleep(0)  # let the fire-and-forget refund publish run

    assert granted is True
    assert reason is None
    refunds = client.refund_commands()
    assert len(refunds) == 1
    assert refunds[0].amount == 1.00
    assert refunds[0].reason == "maintenance"
    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0
    assert vmc.selected_product is None
    hold = machine.maintenance_hold
    assert hold is not None
    assert hold.standby is True
    assert hold.holder_user_id == "user-1"
    assert hold.holder_session_id == "sess-1"
    assert "SVC-102" in _active_fault_codes(vmc)
    machine.cancel_pending_tasks()


async def test_standby_from_idle_refunds_stranded_escrow_and_cancels_timer():
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    assert vmc.state == "idle"
    vmc.deposit_funds(2.00, payment_method="cash_coin")
    assert any(c.label == "session_timeout" for c in runner.scheduled)

    granted, reason = machine.lease.begin_standby("user-1", "sess-1")
    await asyncio.sleep(0)

    assert granted is True
    assert reason is None
    refunds = client.refund_commands()
    assert len(refunds) == 1
    assert refunds[0].amount == 2.00
    assert refunds[0].reason == "maintenance"
    assert vmc.state == "idle"
    assert vmc.credit_escrow == 0.0
    assert not any(c.label == "session_timeout" for c in runner.scheduled)
    assert machine.maintenance_hold is not None
    assert machine.maintenance_hold.standby is True
    machine.cancel_pending_tasks()


async def test_standby_from_error_refunds_and_stays_in_error():
    machine = make_machine2()
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    vmc.machine.set_state("error")
    vmc.credit_escrow = 1.50

    granted, reason = machine.lease.begin_standby("user-1", "sess-1")
    await asyncio.sleep(0)

    assert granted is True
    assert reason is None
    refunds = client.refund_commands()
    assert len(refunds) == 1
    assert refunds[0].amount == 1.50
    assert refunds[0].reason == "maintenance"
    assert vmc.state == "error"  # the fault that parked it there is still admin's
    assert vmc.credit_escrow == 0.0
    assert machine.maintenance_hold is not None
    assert machine.maintenance_hold.standby is True
    machine.cancel_pending_tasks()


async def test_standby_refused_while_dispensing():
    machine = make_machine2()
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    client = RecordingClient()
    machine.set_mqtt_client(client)
    vmc.machine.set_state("dispensing")
    vmc.credit_escrow = 3.00

    granted, reason = machine.lease.begin_standby("user-1", "sess-1")
    await asyncio.sleep(0)

    assert granted is False
    assert reason == "vend finishing, tap again"
    assert client.refund_commands() == []
    assert vmc.credit_escrow == 3.00  # untouched
    assert machine.maintenance_hold is None
    machine.cancel_pending_tasks()


async def test_standby_refused_when_another_session_holds_the_lease():
    machine = make_machine2()
    machine.attach_to_loop(asyncio.get_running_loop())
    granted1, _ = machine.lease.begin_maintenance("owner-1", "sess-a")
    assert granted1 is True

    granted2, reason2 = machine.lease.begin_standby("tech-2", "sess-b")

    assert granted2 is False
    assert reason2 == "held by owner-1"
    assert machine.maintenance_hold.holder_session_id == "sess-a"
    assert machine.maintenance_hold.standby is False
    machine.cancel_pending_tasks()


async def test_standby_upgrades_own_opportunistic_lease_in_place():
    machine = make_machine2()
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_session_liveness(lambda session_id: True)
    granted1, _ = machine.lease.begin_maintenance("user-1", "sess-1")
    assert granted1 is True
    hold_before = machine.maintenance_hold
    idle_task_before = machine.lease.idle_task
    assert idle_task_before is not None

    granted2, reason2 = machine.lease.begin_standby("user-1", "sess-1")
    await asyncio.sleep(0)  # let the cancelled idle task settle

    assert granted2 is True
    assert reason2 is None
    assert machine.maintenance_hold is hold_before  # same object, upgraded in place
    assert machine.maintenance_hold.standby is True
    assert machine.lease.idle_task is None
    assert machine.lease.sweep_task is not None
    assert idle_task_before.cancelled()
    machine.cancel_pending_tasks()


async def test_standby_lease_never_released_by_idle_timer():
    machine = make_machine2()
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_session_liveness(lambda session_id: True)
    granted, _ = machine.lease.begin_standby("user-1", "sess-1")
    assert granted is True
    machine.maintenance_hold.last_activity_at -= (
        machine.lease.MAINTENANCE_IDLE_TIMEOUT_SECONDS + 1
    )

    # A predicate is wired, so begin_standby's grant armed the session
    # sweep, not the idle timer (MaintenanceLease.arm_sweep) -- there is no
    # live "maintenance_idle" scheduled call to fire here at all. This
    # calls the lease's own public `idle_expired()` directly (a public
    # collaborator method, not a VMC private) to prove the belt-and-braces
    # guard inside it: even if some future caller re-armed the idle timer
    # by mistake for a standby lease with a predicate wired, it must still
    # never release the lease.
    machine.lease.idle_expired()

    assert machine.maintenance_hold is not None
    assert "SVC-102" in _active_fault_codes(vmc)
    machine.cancel_pending_tasks()


async def test_sweep_releases_lease_once_holder_session_is_gone(loud_log):
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    live_calls: list[str] = []

    def liveness(session_id: str) -> bool:
        live_calls.append(session_id)
        return len(live_calls) == 1  # live on the first check, gone on the second

    machine.set_session_liveness(liveness)
    granted, _ = machine.lease.begin_standby("user-1", "sess-1")
    assert granted is True

    runner.fire("standby_sweep")  # first sweep: still live
    assert machine.maintenance_hold is not None
    assert live_calls == ["sess-1"]

    runner.fire("standby_sweep")  # second sweep: session gone

    assert machine.maintenance_hold is None
    assert "SVC-102" not in _active_fault_codes(vmc)
    assert any("session_ended" in msg for msg in loud_log)
    machine.cancel_pending_tasks()


async def test_sweep_defers_release_while_a_run_is_in_flight(loud_log):
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    machine.set_session_liveness(lambda session_id: False)  # already gone
    granted, _ = machine.lease.begin_standby("user-1", "sess-1")
    assert granted is True

    with machine.lease.test_run():
        assert machine.maintenance_hold.runs_in_flight == 1

        runner.fire("standby_sweep")

        assert machine.maintenance_hold is not None
        assert machine.maintenance_hold.release_requested is True
        assert machine.maintenance_hold.release_reason == "session_ended"

    # The run's own `finally` (MaintenanceLease.run_finished) performs the
    # deferred release once runs_in_flight settles back to zero, and must
    # attribute it to "session_ended" (the sweep's own reason), not the
    # generic "admin" run_finished falls back to.
    assert machine.maintenance_hold is None
    assert "SVC-102" not in _active_fault_codes(vmc)
    assert any(
        "released (session_ended)" in msg or "cleared (session_ended)" in msg
        for msg in loud_log
    )
    machine.cancel_pending_tasks()


async def test_idle_timer_defers_release_while_a_run_is_in_flight_and_is_attributed(
    loud_log,
):
    # Standby leases are normally released via the session-liveness sweep,
    # not the idle timer -- but with no predicate wired (begin_standby's
    # fallback), the idle timer is the lease's only automatic release path
    # (see MaintenanceLease.idle_expired's docstring), so its own in-flight
    # deferral must be attributed to "idle_timeout", not misreported as
    # "admin" once the run settles.
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    vmc = machine.vmc
    machine.attach_to_loop(asyncio.get_running_loop())
    granted, _ = machine.lease.begin_standby("user-1", "sess-1")
    assert granted is True
    assert machine.lease.idle_task is not None  # no predicate: idle-timer fallback

    with machine.lease.test_run():
        assert machine.maintenance_hold.runs_in_flight == 1
        machine.maintenance_hold.last_activity_at -= (
            machine.lease.MAINTENANCE_IDLE_TIMEOUT_SECONDS + 1
        )

        runner.fire("maintenance_idle")

        assert machine.maintenance_hold is not None
        assert machine.maintenance_hold.release_requested is True
        assert machine.maintenance_hold.release_reason == "idle_timeout"

    # The run's own `finally` performs the deferred release once
    # runs_in_flight settles back to zero, attributed to "idle_timeout".
    assert machine.maintenance_hold is None
    assert "SVC-102" not in _active_fault_codes(vmc)
    assert any(
        "released (idle_timeout)" in msg or "cleared (idle_timeout)" in msg
        for msg in loud_log
    )
    machine.cancel_pending_tasks()


async def test_standby_falls_back_to_idle_timer_when_no_predicate_wired(loud_log):
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    machine.attach_to_loop(asyncio.get_running_loop())

    granted, _ = machine.lease.begin_standby("user-1", "sess-1")

    assert granted is True
    assert machine.maintenance_hold.standby is True
    assert machine.lease.sweep_task is None
    assert machine.lease.idle_task is not None
    assert any("no session-liveness predicate" in msg.lower() for msg in loud_log)

    # With no predicate the idle timer is the only automatic release, so
    # it must still act on a standby lease (the "degrades to the
    # opportunistic lease" half of plan Task 1).
    runner.fire("maintenance_idle")
    assert machine.maintenance_hold is None
    machine.cancel_pending_tasks()


async def test_takeover_of_standby_lease_keeps_standby_and_sweeps_new_session():
    runner = FakeTaskRunner()
    machine = make_machine2(tasks=runner)
    machine.attach_to_loop(asyncio.get_running_loop())
    live_calls: list[str] = []

    def liveness(session_id: str) -> bool:
        live_calls.append(session_id)
        return True

    machine.set_session_liveness(liveness)
    granted, _ = machine.lease.begin_standby("user-1", "sess-a")
    assert granted is True
    machine.maintenance_hold.last_activity_at -= (
        machine.lease.MAINTENANCE_TAKEOVER_IDLE_SECONDS + 1
    )

    granted2, reason2 = machine.lease.take_over_maintenance("user-2", "sess-b")

    assert granted2 is True
    assert reason2 is None
    assert machine.maintenance_hold.standby is True
    assert machine.maintenance_hold.holder_session_id == "sess-b"
    assert machine.lease.idle_task is None
    assert machine.lease.sweep_task is not None

    runner.fire("standby_sweep")
    assert live_calls[-1] == "sess-b"
    machine.cancel_pending_tasks()
