# tests/test_test_sale_runner.py
"""Tests for `controller/test_sale.py`'s `TestSaleRunner` (Task 10 of the
vmc-reduction plan): `run_test_sale` moved out of the VMC verbatim, onto
the public seams Task 9 built (`find_product`/`begin_test_sale`/
`end_test_sale`, `subscribe_state_change`/`subscribe_sale_settled`) plus
the `MaintenanceLease.test_run()` bracket and the `DispenserProfileGate`
CFG-101 pre-check -- unchanged behavior, just a different home
(`machine.test_sales.run_test_sale`, not `vmc.run_test_sale`).

Reuses `tests/test_vmc_observers.py`'s module-level `_vmc_with_profiles`
helper (a `Machine`/`VMC` wired for a real dispatch: loaded dispenser
profiles, a `FakeDispatcher`, a `FakeMqtt`, and a `FakeTaskRunner`) rather
than inventing a new one, per the task brief.
"""

import asyncio

import pytest

from tests.test_vmc_observers import _vmc_with_profiles


class FakeEventRecorder:
    """Minimal recorder -- mirrors `tests/test_vmc_flows.py`'s and
    `tests/test_vmc_dispense_profiles.py`'s own local copies."""

    def __init__(self):
        self.events: list[tuple] = []
        self.sales: list[tuple] = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        self.sales.append((sku, name, slot, price, methods))


def _no_refund_published(client) -> bool:
    return not any(topic == "cmd/payment/refund" for topic, _ in client.published)


async def test_dispensed_sale_returns_outcome_and_writes_ok_test_run_row(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    assert vmc.state == "interacting_with_user"

    vmc.process_payment()
    assert vmc.state == "dispensing"

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )
    result = await asyncio.wait_for(task, timeout=5)

    assert result.sku == product.sku
    assert result.outcome == "dispensed"
    assert result.fault_code is None
    assert result.run_id
    assert result.path[-1] == "idle"

    test_run_events = [e for e in rec.events if e[0] == "test_run"]
    assert len(test_run_events) == 1
    assert test_run_events[0][2]["run_id"] == result.run_id
    assert test_run_events[0][2]["status"] == "ok"

    assert _no_refund_published(client)
    assert machine.lease.hold.runs_in_flight == 0
    machine.cancel_pending_tasks()


async def test_jam_returns_vend_failed_with_code_and_failed_status(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]  # ICE-1, bagged_ice -- jam maps to ICE-401
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    vmc.process_payment()
    assert vmc.state == "dispensing"

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "jam"}
    )
    result = await asyncio.wait_for(task, timeout=5)

    assert result.outcome == "vend_failed"
    assert result.fault_code == "ICE-401"

    test_run_events = [e for e in rec.events if e[0] == "test_run"]
    assert len(test_run_events) == 1
    assert test_run_events[0][2]["status"] == "failed"

    assert _no_refund_published(client)
    assert machine.lease.hold.runs_in_flight == 0
    machine.cancel_pending_tasks()


async def test_dispense_timeout_returns_timeout_with_no_fault_code(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    vmc.process_payment()
    assert vmc.state == "dispensing"
    assert any(c.label == "dispense_timeout" for c in runner.scheduled)

    runner.fire("dispense_timeout")
    result = await asyncio.wait_for(task, timeout=5)

    assert result.outcome == "timeout"
    assert result.fault_code is None

    test_run_events = [e for e in rec.events if e[0] == "test_run"]
    assert len(test_run_events) == 1
    assert test_run_events[0][2]["status"] == "failed"

    assert _no_refund_published(client)
    assert machine.lease.hold.runs_in_flight == 0
    machine.cancel_pending_tasks()


async def test_cancelled_mid_vend_leaves_sale_as_test_and_skips_record_sale(tmp_path):
    """Copilot review, PR #48 (carried from `TestRunTestSale`): cancelling
    the awaiting task mid-vend must not reclassify the in-flight test vend
    as production -- a later `complete` report still writes neither a
    sale nor a `dispense` row, and still publishes no refund."""
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    vmc.process_payment()
    assert vmc.state == "dispensing"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert vmc.state == "dispensing"
    assert vmc.sale is not None and vmc.sale.is_test is True
    assert machine.lease.hold.runs_in_flight == 0

    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )

    assert rec.sales == []
    assert not any(t == "dispense" for t, *_ in rec.events)
    assert _no_refund_published(client)
    machine.cancel_pending_tasks()


async def test_second_concurrent_call_refused_before_lease_bracket_leaves_idle_clock_untouched(
    tmp_path,
):
    """The already-in-progress guard is checked in `run_test_sale` BEFORE
    `self._lease.test_run()` is ever entered (review fix), matching the
    original, pre-extraction ordering: a refused double-submit must never
    refresh the lease's idle clock (`MaintenanceLease.run_started`'s
    `last_activity_at` bump) and must always produce the exact
    "already in progress" message `begin_test_sale` itself uses."""
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]
    other = vmc.products[1]
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task1 = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    assert task1.done() is False
    assert machine.lease.hold.runs_in_flight == 1

    last_activity_before = machine.lease.hold.last_activity_at

    with pytest.raises(
        RuntimeError,
        match=(
            "run_test_sale: a simulated sale is already in progress; "
            "wait for it to finish \\(or time out\\) before starting another"
        ),
    ):
        await asyncio.wait_for(machine.test_sales.run_test_sale(other.sku), timeout=5)

    # The refusal happened before the lease bracket was ever entered, so
    # it must not have touched runs_in_flight or the idle clock.
    assert machine.lease.hold.runs_in_flight == 1
    assert machine.lease.hold.last_activity_at == last_activity_before

    vmc.process_payment()
    assert vmc.state == "dispensing"
    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )
    result = await asyncio.wait_for(task1, timeout=5)

    assert result.outcome == "dispensed"
    assert machine.lease.hold.runs_in_flight == 0
    assert _no_refund_published(client)
    machine.cancel_pending_tasks()


async def test_second_concurrent_call_raises_already_in_progress(tmp_path):
    machine, vmc, dispatcher, client, runner = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    machine.set_event_recorder(rec)
    product = vmc.products[0]
    other = vmc.products[1]
    granted, _ = vmc.begin_maintenance("user-1", "sess-1")
    assert granted is True

    task1 = asyncio.get_running_loop().create_task(
        machine.test_sales.run_test_sale(product.sku)
    )
    await asyncio.sleep(0)
    assert task1.done() is False
    assert machine.lease.hold.runs_in_flight == 1

    with pytest.raises(RuntimeError, match="already in progress"):
        await asyncio.wait_for(machine.test_sales.run_test_sale(other.sku), timeout=5)

    # The refused call must not have perturbed call #1's in-flight state.
    assert task1.done() is False
    assert machine.lease.hold.runs_in_flight == 1

    vmc.process_payment()
    assert vmc.state == "dispensing"
    await vmc.on_dispenser_event(
        "hardware/dispenser", {"slot": product.slot, "state": "complete"}
    )
    result = await asyncio.wait_for(task1, timeout=5)

    assert result.outcome == "dispensed"
    assert machine.lease.hold.runs_in_flight == 0
    assert vmc.test_sale_in_progress is False
    assert _no_refund_published(client)
    machine.cancel_pending_tasks()
