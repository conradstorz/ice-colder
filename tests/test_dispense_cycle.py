# tests/test_dispense_cycle.py
"""Unit tests for `controller.dispense_cycle.DispenseCycle`, in isolation
from `VMC` -- no FSM. `tasks` is the real `tests.fakes.FakeTaskRunner`,
attached to the running event loop so `fire_and_forget` actually runs
(awaited with a bare `asyncio.sleep(0)`) and `schedule()` is fired by
label rather than by poking a private task handle.

`FakeGate` stands in for `controller.dispenser_gate.DispenserProfileGate`
(a fixed `SlotProfile` or `None`), built from a real, loaded
`DispenserProfiles` via `tests.dispenser_fixtures.profiles_for` so the
`SlotProfile`/`DispenseCommand` the cycle builds is the real thing, not a
hand-rolled stub. `FakeDispatcher` (same module) covers a successful ack,
a rejected ack, `CommandTimeout`, and an arbitrary exception. `FakeFaults`
and `FakeRecorder` are small local fakes recording exactly what
`DispenseCycle` hands them, and `on_failed`/`on_request_id` are recording
closures -- every test asserts on these recorded values, never bare call
counts.
"""

from __future__ import annotations

import asyncio

from config.config_model import Product
from contracts.common import CommandAck
from contracts.vending_machine import DispenserOutcome, FaultCode
from controller.dispense_cycle import DispenseCycle, DispenseReport
from controller.outputs import StatusOutputs
from controller.sale_context import SaleContext
from services.command_dispatcher import CommandTimeout
from services.event_recorder import SaleRecordingFailed
from services.session_store import SessionSnapshot
from tests.dispenser_fixtures import FakeDispatcher, profiles_for
from tests.fakes import FakeTaskRunner

ICE = Product(sku="ICE-1", slot=0, kind="ice", name="Ice", price=2.0)
WATER = Product(sku="W-1", slot=1, kind="water", name="Water", price=3.0)


class FakeGate:
    """Stands in for `DispenserProfileGate`: returns a fixed profile (or
    `None`) for every product, regardless of which product is asked."""

    def __init__(self, profile=None):
        self.profile = profile

    def profile_for(self, product):
        return self.profile


class FakeFaults:
    """Stands in for `FaultService`: records every `raise_fault` call."""

    def __init__(self):
        self.raised: list[tuple] = []

    def raise_fault(self, code, *, sku=None, outcome=None):
        self.raised.append((code, sku, outcome))


class FakeRecorder:
    """Stands in for the event recorder: records every `record`/
    `record_sale` call; `fail_with`, when set, is raised by the next
    `record_sale` call instead of recording it."""

    def __init__(self):
        self.events: list[tuple] = []
        self.sales: list[tuple] = []
        self.fail_with: Exception | None = None

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.sales.append((sku, name, slot, price, methods))


class FailureRecorder:
    """Recording `on_failed` closure: an async callable logging every
    `(cycle, code, outcome)` it was invoked with."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, cycle, code, outcome):
        self.calls.append((cycle, code, outcome))


def _profile_for(tmp_path, product) -> object:
    profiles = profiles_for([product], tmp_path)
    return profiles.profile_for_slot(product.slot)


def not_open_snapshot(state: str | None) -> SessionSnapshot:
    return SessionSnapshot(state=state or "idle", credit_escrow=0.0)


def make_outputs(runner: FakeTaskRunner, *, session_store=None) -> StatusOutputs:
    outputs = StatusOutputs(
        snapshot=not_open_snapshot,
        credit_escrow=lambda: 0.0,
        selected_product=lambda: None,
        fsm_state=lambda: "idle",
        pay104_active=lambda: False,
        tasks=runner,
    )
    if session_store is not None:
        outputs.attach_session_store(session_store)
    return outputs


def make_cycle(
    *,
    product,
    profile=None,
    dispatcher=None,
    shares=None,
    recorder=None,
    runner=None,
    outputs=None,
    faults=None,
    on_failed=None,
    on_request_id=None,
    set_transaction_certain=None,
    timeout_seconds=90.0,
) -> tuple[DispenseCycle, FakeTaskRunner]:
    runner = runner if runner is not None else FakeTaskRunner()
    sale = SaleContext(product=product, shares=shares)
    cycle = DispenseCycle(
        sale=sale,
        dispatcher=dispatcher if dispatcher is not None else (lambda: None),
        gate=FakeGate(profile),
        outputs=outputs if outputs is not None else make_outputs(runner),
        faults=faults if faults is not None else FakeFaults(),
        recorder=recorder if recorder is not None else (lambda: None),
        set_transaction_certain=set_transaction_certain or (lambda certain: None),
        tasks=runner,
        timeout_seconds=lambda: timeout_seconds,
        on_failed=on_failed if on_failed is not None else FailureRecorder(),
        on_request_id=on_request_id or (lambda rid, mech: None),
    )
    return cycle, runner


# --- start() ---


async def test_no_profile_fails_with_cfg101():
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    cycle, runner = make_cycle(
        product=ICE, profile=None, runner=runner, on_failed=failures
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.CFG_101, "no_profile")]
    assert cycle.request_id is None
    assert cycle.mechanism is None


class FailingStore:
    """Stub session store whose `save_async` always raises."""

    async def save_async(self, snap):
        raise OSError("disk full")


async def test_snapshot_save_failure_fails_with_pay102(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    outputs = make_outputs(runner, session_store=FailingStore())
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        runner=runner,
        outputs=outputs,
        on_failed=failures,
    )

    cycle.start(lambda state: SessionSnapshot(state=state, credit_escrow=2.0))
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "snapshot_failed")]


async def test_no_dispatcher_fails_with_pay102_no_ack(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: None,
        runner=runner,
        on_failed=failures,
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "no_ack")]


async def test_dispatcher_timeout_fails_with_pay102_no_ack(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    dispatcher = FakeDispatcher()
    dispatcher.fail_with = CommandTimeout("vending", "dispense")
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: dispatcher,
        runner=runner,
        on_failed=failures,
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "no_ack")]


async def test_dispatcher_other_exception_fails_with_pay102_no_ack(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    dispatcher = FakeDispatcher()
    dispatcher.fail_with = RuntimeError("boom")
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: dispatcher,
        runner=runner,
        on_failed=failures,
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "no_ack")]


async def test_rejected_ack_fails_with_pay102_no_ack(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()

    async def send_rejected(subsystem, command, params=None, request_id=None):
        return CommandAck(request_id="rejected-1", command=command, status="rejected")

    dispatcher = FakeDispatcher()
    dispatcher.send = send_rejected
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: dispatcher,
        runner=runner,
        on_failed=failures,
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "no_ack")]


async def test_good_ack_never_fails_and_sets_request_id(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    dispatcher = FakeDispatcher()
    request_ids: list[tuple] = []
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: dispatcher,
        runner=runner,
        on_failed=failures,
        on_request_id=lambda rid, mech: request_ids.append((rid, mech)),
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    assert failures.calls == []
    assert len(request_ids) == 1
    assert request_ids[0][1] == "bagged_ice"
    assert cycle.request_id == request_ids[0][0]
    assert cycle.mechanism == "bagged_ice"
    subsystem, command, params = dispatcher.sent[-1]
    assert subsystem == "vending"
    assert command == "dispense"
    assert params["slot"] == ICE.slot


async def test_timeout_fires_on_failed_no_report(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    failures = FailureRecorder()
    dispatcher = FakeDispatcher()
    gate = asyncio.Event()
    dispatcher.gate = gate  # hold the ack so the timer fires first
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE,
        profile=profile,
        dispatcher=lambda: dispatcher,
        runner=runner,
        on_failed=failures,
    )

    cycle.start(lambda state: None)
    await asyncio.sleep(0)

    runner.fire("dispense_timeout")
    await asyncio.sleep(0)

    assert failures.calls == [(cycle, FaultCode.PAY_102, "no_report")]
    gate.set()
    await asyncio.sleep(0)


async def test_cancel_retires_the_timer(tmp_path):
    runner = FakeTaskRunner()
    runner.attach(asyncio.get_running_loop())
    dispatcher = FakeDispatcher()
    profile = _profile_for(tmp_path, ICE)
    cycle, runner = make_cycle(
        product=ICE, profile=profile, dispatcher=lambda: dispatcher, runner=runner
    )

    cycle.start(lambda state: None)
    assert any(c.label == "dispense_timeout" for c in runner.scheduled)

    cycle.cancel()

    assert not any(c.label == "dispense_timeout" for c in runner.scheduled)
    await asyncio.sleep(0)


# --- classify() ---


def _started_cycle(tmp_path, product, mechanism_profile) -> DispenseCycle:
    """A cycle with `start()` already run (synchronously, no loop
    attached -- the dispatch task is created and immediately closed) so
    `request_id`/`mechanism` are set, exactly as they would be before any
    `hardware/dispenser` report could arrive."""
    runner = FakeTaskRunner()  # no loop attached: fire_and_forget is a no-op
    cycle, _ = make_cycle(product=product, profile=mechanism_profile, runner=runner)
    cycle.start(lambda state: None)
    return cycle


def test_classify_non_terminal_state_returns_none(tmp_path, caplog):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    assert cycle.classify({"state": "agitate", "slot": ICE.slot}) is None


def test_classify_slot_mismatch_returns_none(tmp_path):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    report = cycle.classify(
        {"state": "complete", "slot": ICE.slot + 1, "request_id": cycle.request_id}
    )

    assert report is None


def test_classify_request_id_mismatch_returns_none(tmp_path):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    report = cycle.classify(
        {"state": "complete", "slot": ICE.slot, "request_id": "some-other-id"}
    )

    assert report is None


def test_classify_accepts_report_with_no_request_id(tmp_path):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    report = cycle.classify({"state": "complete", "slot": ICE.slot})

    assert report == DispenseReport(
        outcome=DispenserOutcome.complete, success=True, fault=None
    )


def test_classify_complete_is_success(tmp_path):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    report = cycle.classify(
        {"state": "complete", "slot": ICE.slot, "request_id": cycle.request_id}
    )

    assert report.success is True
    assert report.fault is None


def test_classify_door_open_on_bagged_ice_is_success_with_ice402(tmp_path):
    profile = _profile_for(tmp_path, ICE)
    cycle = _started_cycle(tmp_path, ICE, profile)

    report = cycle.classify(
        {"state": "door_open", "slot": ICE.slot, "request_id": cycle.request_id}
    )

    assert report.success is True
    assert report.fault is FaultCode.ICE_402


def test_classify_door_open_on_water_fill_is_failure(tmp_path):
    profile = _profile_for(tmp_path, WATER)
    cycle = _started_cycle(tmp_path, WATER, profile)

    report = cycle.classify(
        {"state": "door_open", "slot": WATER.slot, "request_id": cycle.request_id}
    )

    assert report.success is False
    assert report.fault is FaultCode.ICE_302  # fallback: (water_fill, error)


def test_classify_jam_on_water_fill_maps_to_mechanism_error_code(tmp_path, caplog):
    profile = _profile_for(tmp_path, WATER)
    cycle = _started_cycle(tmp_path, WATER, profile)

    report = cycle.classify(
        {"state": "jam", "slot": WATER.slot, "request_id": cycle.request_id}
    )

    assert report.success is False
    assert report.fault is FaultCode.ICE_302
    assert any("no fault mapped" in r.message.lower() for r in caplog.records)


# --- record() ---


async def test_record_writes_sale_with_share_sum_price():
    recorder = FakeRecorder()
    cycle, _ = make_cycle(
        product=ICE,
        shares={"cash": 1.00, "card": 0.75},
        recorder=lambda: recorder,
    )

    await cycle.record()

    assert recorder.events == [("dispense", float(ICE.slot), None)]
    assert recorder.sales == [
        (ICE.sku, ICE.name, ICE.slot, 1.75, {"cash": 1.00, "card": 0.75})
    ]


async def test_record_skipped_without_a_recorder():
    cycle, _ = make_cycle(product=ICE, recorder=lambda: None)

    await cycle.record()  # must not raise


async def test_record_defaults_price_to_product_price_without_shares():
    recorder = FakeRecorder()
    cycle, _ = make_cycle(product=ICE, shares=None, recorder=lambda: recorder)

    await cycle.record()

    assert recorder.sales == [
        (
            ICE.sku,
            ICE.name,
            ICE.slot,
            round(ICE.price, 2),
            {"unknown": round(ICE.price, 2)},
        )
    ]


async def test_record_raises_data101_on_generic_failure():
    recorder = FakeRecorder()
    recorder.fail_with = RuntimeError("boom")
    faults = FakeFaults()
    cycle, _ = make_cycle(
        product=ICE,
        shares={"cash": 2.0},
        recorder=lambda: recorder,
        faults=faults,
    )

    await cycle.record()  # must not raise

    assert faults.raised == [(FaultCode.DATA_101, None, "sku=ICE-1 price=$2.00")]


async def test_record_raises_pay104_and_sets_transaction_uncertain():
    recorder = FakeRecorder()
    recorder.fail_with = SaleRecordingFailed("nowhere durable")
    faults = FakeFaults()
    certain_calls: list[bool] = []
    cycle, _ = make_cycle(
        product=ICE,
        shares={"cash": 2.0},
        recorder=lambda: recorder,
        faults=faults,
        set_transaction_certain=certain_calls.append,
    )

    await cycle.record()  # must not raise

    assert certain_calls == [False]
    assert faults.raised == [
        (FaultCode.PAY_104, None, "sku=ICE-1 price=$2.00 unrecorded")
    ]
