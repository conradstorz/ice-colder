"""Unit tests for `ice_edge.sale.session_evidence.SessionEvidence`, in
isolation from `Machine` and `VMC` -- no FSM, no MQTT, no health monitor. Uses
a real `SessionStore` pointed at a `tmp_path` file, hand-built
`SessionSnapshot`s and small recording fakes for faults and payments.
"""

import asyncio

from ice_contracts.board.vending_machine import FaultCode
from ice_edge.faults.fault_service import FaultChange
from ice_edge.ledger.session_store import SessionSnapshot, SessionStore
from ice_edge.sale.session_evidence import (
    BootDecision,
    DiscardOutcome,
    RecordOutcome,
    RecoveryDetail,
    SessionEvidence,
)


class FakeFaults:
    """Records raises; `clear_fault` returns `clear_ok`, and a successful clear
    removes the fault from `active_faults`."""

    def __init__(self, *, active=(), clear_ok=True, on_clear=None):
        self.raised: list[tuple[FaultCode, str | None]] = []
        self.cleared: list[tuple[str, str]] = []
        self.active = [{"code": c.value} for c in active]
        self.clear_ok = clear_ok
        self.on_clear = on_clear

    def raise_fault(self, code, *, sku=None, outcome=None):
        self.raised.append((code, outcome))
        if not any(f["code"] == code.value for f in self.active):
            self.active.append({"code": code.value})

    def clear_fault(self, key, by="admin"):
        self.cleared.append((key, by))
        if self.clear_ok:
            self.active = [f for f in self.active if f["code"] != key]
            if self.on_clear:
                self.on_clear()
        return self.clear_ok

    def active_faults(self):
        return list(self.active)

    def codes(self):
        return [c for c, _ in self.raised]


class FakePayments:
    def __init__(self, *, fail=False):
        self.cash_sales: list[tuple[int, str | None]] = []
        self.fail = fail

    def cash_sale(self, cents, sale_id):
        if self.fail:
            raise RuntimeError("not queued")
        self.cash_sales.append((cents, sale_id))


class FakeAvailability:
    def __init__(self):
        self.certain: list[bool] = []

    def set_transaction_certain(self, certain):
        self.certain.append(certain)


class FakeRecorder:
    def __init__(self, *, fail=False):
        self.sales: list[tuple] = []
        self.events: list[tuple] = []
        self.fail = fail

    def record_sale(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("db down")
        self.sales.append((args, kwargs))

    def record(self, kind, **kwargs):
        self.events.append((kind, kwargs))


def make_recovery(
    store: SessionStore | None,
    *,
    evidence_held: bool = False,
    sale_recorded=None,
    faults=None,
    payments=None,
    recorder=None,
    availability=None,
    seed_payouts=None,
    seed_event_ids=None,
):
    names: dict[str, str] = {"ICE-1": "Ice Bag"}

    def product_name(sku):
        if sku is None:
            return None
        return names.get(sku, sku)

    extra = {} if sale_recorded is None else {"sale_recorded": sale_recorded}
    if seed_payouts is not None:
        extra["seed_payouts"] = seed_payouts
    if seed_event_ids is not None:
        extra["seed_event_ids"] = seed_event_ids
    evidence = SessionEvidence(
        store=lambda: store,
        product_name=product_name,
        faults=faults or FakeFaults(),
        payments=payments or FakePayments(),
        recorder=lambda: recorder,
        availability=lambda: availability,
        **extra,
    )
    if evidence_held:
        evidence.hold()
    return evidence


# --- evaluate_at_boot ---


def test_evaluate_at_boot_none_with_no_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    recovery = make_recovery(store)

    decision = recovery.evaluate_at_boot()

    assert decision == BootDecision(kind="none", snapshot=None)


def test_evaluate_at_boot_closed_clears_the_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(state="idle", credit_escrow_cents=0)
    store.save(snap)
    recovery = make_recovery(store)

    decision = recovery.evaluate_at_boot()

    assert decision.kind == "closed"
    assert decision.snapshot == snap
    assert store.load() is None


def test_evaluate_at_boot_discard_test_clears_the_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=0,
        selected_sku="ICE-1",
        pending_sale_shares={"test": 250},
        is_test=True,
    )
    store.save(snap)
    recovery = make_recovery(store)

    decision = recovery.evaluate_at_boot()

    assert decision.kind == "discard_test"
    assert decision.snapshot == snap
    assert store.load() is None


def test_evaluate_at_boot_uncertain_leaves_the_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=250,
        selected_sku="ICE-1",
        pending_sale_shares={"cash_bill": 250},
    )
    store.save(snap)
    recovery = make_recovery(store)

    # Capture the file content before evaluate_at_boot
    file_path = tmp_path / "session.json"
    bytes_before = file_path.read_bytes()
    snapshot_before = store.load()

    decision = recovery.evaluate_at_boot()

    # Verify the decision is correct
    assert decision.kind == "uncertain"
    assert decision.snapshot == snap

    # Verify the file is unchanged (both content and loaded snapshot)
    bytes_after = file_path.read_bytes()
    assert bytes_before == bytes_after, "Raw file bytes should be unchanged"
    assert store.load() == snapshot_before, "Loaded snapshot should be unchanged"


# --- pending_sale_for_recovery ---


def test_pending_sale_for_recovery_none_when_pay104_not_active(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 250},
        )
    )
    recovery = make_recovery(store, evidence_held=False)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_none_for_test_snapshot(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"test": 250},
            is_test=True,
        )
    )
    recovery = make_recovery(store, evidence_held=True)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_none_when_shares_empty(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="idle",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares=None,
        )
    )
    recovery = make_recovery(store, evidence_held=True)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_returns_dict_when_everything_lines_up(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=0,
        selected_sku="ICE-1",
        dispense_slot=3,
        pending_sale_shares={"cash_bill": 200, "card": 50},
    )
    store.save(snap)
    recovery = make_recovery(store, evidence_held=True)

    pending = recovery.pending_sale_for_recovery()

    assert pending == {
        "sku": "ICE-1",
        "name": "Ice Bag",
        "slot": 3,
        "price_cents": 250,
        "methods": {"cash_bill": 200, "card": 50},
        "saved_at": snap.saved_at,
    }


# --- reserve_pending_sale / pending_sale_already_recorded ---


def test_reserve_then_already_recorded_true_for_same_pending(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 250},
        )
    )
    recovery = make_recovery(store, evidence_held=True)
    pending = recovery.pending_sale_for_recovery()

    assert recovery.pending_sale_already_recorded(pending) is False
    recovery.reserve_pending_sale(pending)
    assert recovery.pending_sale_already_recorded(pending) is True


def test_already_recorded_false_for_different_saved_at(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 250},
        )
    )
    recovery = make_recovery(store, evidence_held=True)
    pending = recovery.pending_sale_for_recovery()
    recovery.reserve_pending_sale(pending)

    different = dict(pending, saved_at=pending["saved_at"] + 1.0)

    assert recovery.pending_sale_already_recorded(different) is False


# --- mark_pending_sale_recorded ---


def test_mark_pending_sale_recorded_clears_shares_on_disk(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 250},
        )
    )
    recovery = make_recovery(store, evidence_held=True)
    assert recovery.pending_sale_for_recovery() is not None

    assert recovery.mark_pending_sale_recorded() is True

    assert recovery.pending_sale_for_recovery() is None
    reloaded = store.load()
    assert reloaded.pending_sale_shares is None


def test_mark_pending_sale_recorded_false_with_no_store():
    recovery = make_recovery(None, evidence_held=True)

    assert recovery.mark_pending_sale_recorded() is False


# --- already-recorded sale (M5) ---


def _open_snapshot(**kw):
    return SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=0,
        selected_sku="ICE-1",
        pending_sale_shares={"cash": 250},
        **kw,
    )


def test_uncertain_snapshot_whose_sale_is_recorded_is_closed(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = _open_snapshot()
    store.save(snap)
    calls = []

    def recorded(sku, since_ts, methods, window):
        calls.append((sku, since_ts, methods))
        return True

    decision = make_recovery(store, sale_recorded=recorded).evaluate_at_boot()

    assert decision.kind == "closed"
    assert store.load() is None
    assert calls == [("ICE-1", snap.saved_at, {"cash": 250})]


def test_uncertain_snapshot_not_recorded_stays_uncertain(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot())

    decision = make_recovery(store, sale_recorded=lambda *a: False).evaluate_at_boot()

    assert decision.kind == "uncertain"
    assert store.load() is not None


def test_test_snapshot_never_consults_sale_recorded(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot(is_test=True))

    def boom(*a):
        raise AssertionError("must not be consulted")

    decision = make_recovery(store, sale_recorded=boom).evaluate_at_boot()

    assert decision.kind == "discard_test"


def test_pending_sale_for_recovery_none_when_row_exists(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot())
    recovery = make_recovery(store, evidence_held=True, sale_recorded=lambda *a: True)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_returned_when_row_absent(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot())
    recovery = make_recovery(store, evidence_held=True, sale_recorded=lambda *a: False)

    assert recovery.pending_sale_for_recovery()["sku"] == "ICE-1"


def test_recorded_sale_with_leftover_credit_stays_uncertain(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot_with(credit_escrow_cents=250))
    recovery = make_recovery(store, evidence_held=True, sale_recorded=lambda *a: True)

    assert recovery.evaluate_at_boot().kind == "uncertain"
    assert store.load() is not None
    assert recovery.pending_sale_for_recovery() is None


def test_recorded_sale_with_zero_credit_is_closed(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot_with(credit_escrow_cents=0))

    decision = make_recovery(store, sale_recorded=lambda *a: True).evaluate_at_boot()

    assert decision.kind == "closed"
    assert store.load() is None


def _open_snapshot_with(**kw):
    return SessionSnapshot(
        state="dispensing",
        selected_sku="ICE-1",
        pending_sale_shares={"cash": 250},
        **{"credit_escrow_cents": 0, **kw},
    )


# --- recovery_detail (M11) ---


def test_recovery_detail_none_without_store():
    assert make_recovery(None).recovery_detail() is None


def test_recovery_detail_none_without_snapshot(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    assert make_recovery(store).recovery_detail() is None


def test_recovery_detail_reports_leftover_credit(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        _open_snapshot_with(
            credit_escrow_cents=250,
            pending_payouts=[
                {"request_id": "p1", "kind": "payout", "cents": 100, "reason": "change"}
            ],
            error="bad json",
        )
    )
    detail = make_recovery(
        store, evidence_held=True, sale_recorded=lambda *a: True
    ).recovery_detail()

    assert detail == RecoveryDetail(
        sku="ICE-1",
        sale_recorded=True,
        credit_escrow_cents=250,
        pending_payouts=[
            {"request_id": "p1", "kind": "payout", "cents": 100, "reason": "change"}
        ],
        error="bad json",
        is_test=False,
    )


def test_recovery_detail_not_recorded(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot_with())
    detail = make_recovery(
        store, evidence_held=True, sale_recorded=lambda *a: False
    ).recovery_detail()

    assert detail is not None
    assert detail.sale_recorded is False
    assert detail.credit_escrow_cents == 0


def test_recovery_detail_test_snapshot(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot_with(is_test=True))
    detail = make_recovery(store, evidence_held=True).recovery_detail()

    assert detail is not None
    assert detail.is_test is True


def test_match_window_is_passed_to_sale_recorded(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot_with())
    seen = []
    recovery = SessionEvidence(
        store=lambda: store,
        product_name=lambda s: s,
        faults=FakeFaults(),
        payments=FakePayments(),
        sale_recorded=lambda sku, ts, methods, window: seen.append(window) or False,
        match_window_seconds=lambda: 777.0,
    )
    recovery.evaluate_at_boot()

    assert seen == [777.0]


def test_unreadable_pre_cents_snapshot_still_raises_uncertain_at_boot(tmp_path):
    """A pre-cents session.json (dollar keys) is no longer converted: it loads
    as an unreadable snapshot, which still evaluates to `uncertain` (PAY-104)
    and keeps its file."""
    import json

    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "state": "interacting_with_user",
                "credit_escrow": 2.5,
                "selected_sku": "ICE-1",
                "credits": [{"method": "cash", "amount": 2.5, "ts": 1.0}],
                "pending_sale_shares": None,
                "saved_at": 100.0,
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(path)

    decision = make_recovery(store).evaluate_at_boot()

    assert decision.kind == "uncertain"
    assert decision.snapshot.error
    assert path.exists()


def test_recovery_detail_and_card_are_none_without_held_evidence(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_coin": 250},
        )
    )
    recovery = make_recovery(store, evidence_held=False)
    assert recovery.recovery_detail() is None
    assert recovery.pending_sale_for_recovery() is None


# --- the hold flag ---


def test_hold_and_release_toggle_held():
    evidence = make_recovery(None)
    assert evidence.held is False
    evidence.hold()
    assert evidence.held is True
    evidence.release()
    assert evidence.held is False


# --- boot entry point ---


def test_recover_at_boot_none_does_nothing(tmp_path):
    faults = FakeFaults()
    evidence = make_recovery(SessionStore(tmp_path / "s.json"), faults=faults)

    assert evidence.recover_at_boot().kind == "none"
    assert faults.raised == []
    assert evidence.held is False


def test_recover_at_boot_discard_test_raises_nothing(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.save(_open_snapshot(is_test=True))
    faults = FakeFaults()
    evidence = make_recovery(store, faults=faults)

    assert evidence.recover_at_boot().kind == "discard_test"
    assert faults.raised == []
    assert evidence.held is False
    assert store.load() is None


def test_recover_at_boot_uncertain_raises_pay104_and_holds(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    snap = _open_snapshot()
    store.save(snap)
    faults, avail, recorder = FakeFaults(), FakeAvailability(), FakeRecorder()
    evidence = make_recovery(
        store, faults=faults, availability=avail, recorder=recorder
    )

    assert evidence.recover_at_boot().kind == "uncertain"

    assert faults.codes() == [FaultCode.PAY_104]
    assert evidence.held is True
    assert avail.certain == [False]
    assert [kind for kind, _ in recorder.events] == ["session_uncertain"]
    assert store.load() == snap  # evidence left in place


def test_recover_at_boot_seeds_the_snapshots_deposit_event_ids(tmp_path):
    from ice_edge.ledger.session_store import Credit

    store = SessionStore(tmp_path / "s.json")
    store.save(
        SessionSnapshot(
            state="interacting_with_user",
            credit_escrow_cents=100,
            credits=[Credit("cash", 100, 1.0, event_id="b-0001")],
        )
    )
    seeded = []

    make_recovery(
        store, seed_event_ids=lambda ids: seeded.extend(ids)
    ).recover_at_boot()

    assert seeded == ["b-0001"]


def test_recover_at_boot_closed_with_pending_sale_sends_cash_sale(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.save(
        SessionSnapshot(
            state="idle",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 250, "card": 50},
        )
    )
    payments, faults = FakePayments(), FakeFaults()

    decision = make_recovery(
        store, payments=payments, faults=faults, sale_recorded=lambda *a: True
    ).recover_at_boot()

    assert decision.kind == "closed"
    assert payments.cash_sales == [(250, None)]
    assert faults.raised == []
    assert store.load() is None


# --- fault-cleared requeue ---


def _uncertain_with_payouts(tmp_path, payouts):
    store = SessionStore(tmp_path / "s.json")
    store.save(_open_snapshot(pending_payouts=payouts))
    return store


def _change(action, code):
    return FaultChange(action=action, code=code, sku=None, by="admin")


def test_pay104_cleared_requeues_boot_payouts_once(tmp_path):
    store = _uncertain_with_payouts(
        tmp_path,
        [{"request_id": "p1", "kind": "payout", "cents": 100, "reason": "change"}],
    )
    seeded = []
    evidence = make_recovery(store, seed_payouts=lambda items: seeded.append(items))
    evidence.recover_at_boot()
    assert seeded == []

    evidence.on_fault_change(_change("cleared", FaultCode.PAY_104))
    evidence.on_fault_change(_change("cleared", FaultCode.PAY_104))

    assert [[p.request_id for p in items] for items in seeded] == [["p1"]]


def test_other_fault_changes_do_not_requeue(tmp_path):
    store = _uncertain_with_payouts(
        tmp_path,
        [{"request_id": "p1", "kind": "payout", "cents": 100, "reason": "change"}],
    )
    seeded = []
    evidence = make_recovery(store, seed_payouts=lambda items: seeded.append(items))
    evidence.recover_at_boot()

    evidence.on_fault_change(_change("cleared", FaultCode.PAY_101))
    evidence.on_fault_change(_change("raised", FaultCode.PAY_104))

    assert seeded == []


def test_malformed_boot_payout_is_dropped_and_raises_pay103(tmp_path):
    store = _uncertain_with_payouts(tmp_path, [{"request_id": "bad"}])
    faults, seeded = FakeFaults(), []
    evidence = make_recovery(
        store, faults=faults, seed_payouts=lambda items: seeded.append(items)
    )
    evidence.recover_at_boot()

    evidence.on_fault_change(_change("cleared", FaultCode.PAY_104))

    assert FaultCode.PAY_103 in faults.codes()
    assert seeded == [[]]


# --- Record as sale ---


def _held_with_pending(tmp_path, **kw):
    store = SessionStore(tmp_path / "session.json")
    store.save(_open_snapshot())
    faults = kw.pop("faults", None) or FakeFaults(active=[FaultCode.PAY_104])
    return store, faults, make_recovery(store, evidence_held=True, faults=faults, **kw)


async def test_record_nothing_pending(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    faults = FakeFaults(active=[FaultCode.PAY_104])
    evidence = make_recovery(store, evidence_held=True, faults=faults)
    recorder = FakeRecorder()

    assert await evidence.record_pending_sale(recorder) is RecordOutcome.NOTHING_PENDING
    assert recorder.sales == []
    assert faults.cleared == []


async def test_record_recorded_writes_idempotently_then_clears(tmp_path):
    payments = FakePayments()
    store, faults, evidence = _held_with_pending(tmp_path, payments=payments)
    pending = evidence.pending_sale_for_recovery()
    recorder = FakeRecorder()

    assert await evidence.record_pending_sale(recorder) is RecordOutcome.RECORDED

    ((args, kwargs),) = recorder.sales
    assert args == ("ICE-1", "Ice Bag", None, 250, {"cash": 250})
    assert kwargs == {"ts": pending["saved_at"], "idempotent": True}
    assert payments.cash_sales == [(250, None)]
    assert faults.cleared == [("PAY-104", "admin")]
    assert faults.raised == []


async def test_record_without_recorder_after_the_pending_checks(tmp_path):
    _, faults, evidence = _held_with_pending(tmp_path)

    assert await evidence.record_pending_sale(None) is RecordOutcome.NO_RECORDER
    assert faults.cleared == []


async def test_record_failed_raises_data101_and_leaves_pay104(tmp_path):
    _, faults, evidence = _held_with_pending(tmp_path)

    outcome = await evidence.record_pending_sale(FakeRecorder(fail=True))

    assert outcome is RecordOutcome.RECORD_FAILED
    assert faults.codes() == [FaultCode.DATA_101]
    assert faults.cleared == []


async def test_record_cash_sale_not_queued_raises_pay103(tmp_path):
    _, faults, evidence = _held_with_pending(tmp_path, payments=FakePayments(fail=True))
    recorder = FakeRecorder()

    outcome = await evidence.record_pending_sale(recorder)

    assert outcome is RecordOutcome.CASH_SALE_NOT_QUEUED
    assert faults.codes() == [FaultCode.PAY_103]
    assert faults.cleared == []
    assert len(recorder.sales) == 1


async def test_record_clear_failed_marker_written(tmp_path):
    store, faults, evidence = _held_with_pending(
        tmp_path, faults=FakeFaults(active=[FaultCode.PAY_104], clear_ok=False)
    )

    outcome = await evidence.record_pending_sale(FakeRecorder())

    assert outcome is RecordOutcome.CLEAR_FAILED_MARKER_WRITTEN
    assert store.load().pending_sale_shares is None
    assert evidence.pending_sale_for_recovery() is None


async def test_record_clear_failed_marker_unwritable_then_already_reserved(
    tmp_path, monkeypatch
):
    store, faults, evidence = _held_with_pending(
        tmp_path, faults=FakeFaults(active=[FaultCode.PAY_104], clear_ok=False)
    )

    def save_fails(snap):
        raise OSError("disk")

    monkeypatch.setattr(store, "save", save_fails)
    recorder = FakeRecorder()

    outcome = await evidence.record_pending_sale(recorder)

    assert outcome is RecordOutcome.MARKER_UNWRITABLE
    assert faults.codes() == [FaultCode.DATA_101]
    # The in-process guard now short-circuits a retry without a second write.
    retry = await evidence.record_pending_sale(recorder)
    assert retry is RecordOutcome.ALREADY_RESERVED
    assert len(recorder.sales) == 1


async def test_concurrent_records_write_one_row(tmp_path):
    faults = FakeFaults(active=[FaultCode.PAY_104])
    _, faults, evidence = _held_with_pending(tmp_path, faults=faults)
    faults.on_clear = evidence.release  # a real clear releases the evidence
    recorder = FakeRecorder()

    outcomes = await asyncio.gather(
        evidence.record_pending_sale(recorder),
        evidence.record_pending_sale(recorder),
    )

    assert sorted(o.value for o in outcomes) == ["nothing_pending", "recorded"]
    assert len(recorder.sales) == 1


# --- Discard ---


async def test_discard_not_active_is_a_noop():
    faults = FakeFaults()
    evidence = make_recovery(None, faults=faults)

    assert await evidence.discard() is DiscardOutcome.NOT_ACTIVE
    assert faults.cleared == []


async def test_discard_clears_pay104():
    faults = FakeFaults(active=[FaultCode.PAY_104])
    evidence = make_recovery(None, faults=faults)

    assert await evidence.discard() is DiscardOutcome.CLEARED
    assert faults.cleared == [("PAY-104", "admin")]


async def test_discard_clear_failed():
    faults = FakeFaults(active=[FaultCode.PAY_104], clear_ok=False)
    evidence = make_recovery(None, faults=faults)

    assert await evidence.discard() is DiscardOutcome.COULD_NOT_CLEAR


# --- plain Clear refusal ---


def test_plain_clear_refused_only_while_a_pending_sale_exists(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    evidence = make_recovery(store, evidence_held=True)
    assert evidence.plain_clear_refused() is False

    store.save(_open_snapshot())
    assert evidence.plain_clear_refused() is True

    evidence.release()
    assert evidence.plain_clear_refused() is False


# --- Machine wiring: requeue runs before the reconciler ---


async def test_machine_requeues_boot_payouts_before_reconciler_runs(
    tmp_path, monkeypatch
):
    from tests.edge.app.test_machine_payments import build

    path = tmp_path / "session.json"
    SessionStore(path).save(
        _open_snapshot(
            pending_payouts=[
                {
                    "request_id": "cs1",
                    "kind": "cash_sale",
                    "cents": 250,
                    "sale_id": None,
                }
            ]
        )
    )
    machine, *_ = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has(FaultCode.PAY_104)

    order: list[str] = []
    real_seed = machine.gateway.seed_payouts
    real_run = machine.gateway.reconciler.maybe_run

    def seed(items):
        order.append("requeue")
        real_seed(items)

    def run():
        order.append("reconcile")
        real_run()

    monkeypatch.setattr(machine.gateway, "seed_payouts", seed)
    monkeypatch.setattr(machine.gateway.reconciler, "maybe_run", run)

    machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin")

    assert order[:2] == ["requeue", "reconcile"]
