# tests/test_session_recovery.py
"""Unit tests for `controller.session_recovery.SessionRecovery`, in
isolation from `VMC` -- no FSM, no MQTT, no health monitor. Uses a real
`SessionStore` pointed at a `tmp_path` file and hand-built
`SessionSnapshot`s.
"""

from controller.session_recovery import BootDecision, SessionRecovery
from services.session_store import SessionSnapshot, SessionStore


def make_recovery(store: SessionStore | None, *, pay104_active: bool = False):
    names: dict[str, str] = {"ICE-1": "Ice Bag"}

    def product_name(sku):
        if sku is None:
            return None
        return names.get(sku, sku)

    return SessionRecovery(
        store=lambda: store,
        product_name=product_name,
        pay104_active=lambda: pay104_active,
    )


# --- evaluate_at_boot ---


def test_evaluate_at_boot_none_with_no_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    recovery = make_recovery(store)

    decision = recovery.evaluate_at_boot()

    assert decision == BootDecision(kind="none", snapshot=None)


def test_evaluate_at_boot_closed_clears_the_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(state="idle", credit_escrow=0.0)
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
        credit_escrow=0.0,
        selected_sku="ICE-1",
        pending_sale_shares={"test": 2.50},
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
        credit_escrow=2.50,
        selected_sku="ICE-1",
        pending_sale_shares={"cash_bill": 2.50},
    )
    store.save(snap)
    recovery = make_recovery(store)

    decision = recovery.evaluate_at_boot()

    assert decision.kind == "uncertain"
    assert decision.snapshot == snap
    assert store.load() is not None


# --- pending_sale_for_recovery ---


def test_pending_sale_for_recovery_none_when_pay104_not_active(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 2.50},
        )
    )
    recovery = make_recovery(store, pay104_active=False)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_none_for_test_snapshot(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares={"test": 2.50},
            is_test=True,
        )
    )
    recovery = make_recovery(store, pay104_active=True)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_none_when_shares_empty(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="idle",
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares=None,
        )
    )
    recovery = make_recovery(store, pay104_active=True)

    assert recovery.pending_sale_for_recovery() is None


def test_pending_sale_for_recovery_returns_dict_when_everything_lines_up(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow=0.0,
        selected_sku="ICE-1",
        dispense_slot=3,
        pending_sale_shares={"cash_bill": 2.00, "card": 0.50},
    )
    store.save(snap)
    recovery = make_recovery(store, pay104_active=True)

    pending = recovery.pending_sale_for_recovery()

    assert pending == {
        "sku": "ICE-1",
        "name": "Ice Bag",
        "slot": 3,
        "price": 2.50,
        "methods": {"cash_bill": 2.00, "card": 0.50},
        "saved_at": snap.saved_at,
    }


# --- reserve_pending_sale / pending_sale_already_recorded ---


def test_reserve_then_already_recorded_true_for_same_pending(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 2.50},
        )
    )
    recovery = make_recovery(store, pay104_active=True)
    pending = recovery.pending_sale_for_recovery()

    assert recovery.pending_sale_already_recorded(pending) is False
    recovery.reserve_pending_sale(pending)
    assert recovery.pending_sale_already_recorded(pending) is True


def test_already_recorded_false_for_different_saved_at(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(
        SessionSnapshot(
            state="dispensing",
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 2.50},
        )
    )
    recovery = make_recovery(store, pay104_active=True)
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
            credit_escrow=0.0,
            selected_sku="ICE-1",
            pending_sale_shares={"cash_bill": 2.50},
        )
    )
    recovery = make_recovery(store, pay104_active=True)
    assert recovery.pending_sale_for_recovery() is not None

    assert recovery.mark_pending_sale_recorded() is True

    assert recovery.pending_sale_for_recovery() is None
    reloaded = store.load()
    assert reloaded.pending_sale_shares is None


def test_mark_pending_sale_recorded_false_with_no_store():
    recovery = make_recovery(None, pay104_active=True)

    assert recovery.mark_pending_sale_recorded() is False
