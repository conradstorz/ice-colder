import json
import time
from pathlib import Path

from loguru import logger

from ice_edge.ledger.session_store import (
    SAVED_AT_FUTURE_SKEW_SECONDS,
    Credit,
    SessionSnapshot,
    SessionStore,
)


def test_round_trip(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=0,
        selected_sku="ICE-1",
        dispense_slot=2,
        dispense_started_at=123.0,
    )
    store.save(snap)
    loaded = store.load()
    assert loaded == snap
    assert not (tmp_path / "session.json.tmp").exists()


def test_round_trip_with_credits_and_pending_shares(tmp_path):
    """Credits must round-trip as Credit instances, not the plain dicts that
    asdict()/json flattened them to on save, and pending_sale_shares must
    survive too."""
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=50,
        selected_sku="ICE-1",
        dispense_slot=2,
        dispense_started_at=123.0,
        credits=[
            Credit(method="cash_bill", cents=200, ts=10.0),
            Credit(method="card", cents=50, ts=20.0),
        ],
        pending_sale_shares={"cash_bill": 200, "card": 50},
    )

    store.save(snap)
    loaded = store.load()

    assert loaded == snap
    assert all(isinstance(c, Credit) for c in loaded.credits)
    assert loaded.credits[0].method == "cash_bill"
    assert loaded.credits[1].cents == 50
    assert loaded.pending_sale_shares == {"cash_bill": 200, "card": 50}


def test_round_trip_with_empty_credits_and_none_shares(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(state="idle", credit_escrow_cents=0)

    store.save(snap)
    loaded = store.load()

    assert loaded.credits == []
    assert loaded.pending_sale_shares is None


def test_old_file_with_no_credits_key_still_loads_with_defaults(tmp_path):
    """A session.json written before this field existed has no "credits" or
    "pending_sale_shares" key at all — SessionSnapshot(**raw) must fill both
    in from their defaults rather than raising (which load() would otherwise
    silently convert into an "unreadable session" error snapshot)."""
    path = tmp_path / "session.json"
    old_style = {
        "state": "interacting_with_user",
        "credit_escrow_cents": 125,
        "selected_sku": "ICE-1",
        "dispense_slot": None,
        "dispense_started_at": None,
        "saved_at": 100.0,
        "error": None,
    }
    path.write_text(json.dumps(old_style), encoding="utf-8")

    loaded = SessionStore(path).load()

    assert loaded.error is None
    assert loaded.credit_escrow_cents == 125
    assert loaded.credits == []
    assert loaded.pending_sale_shares is None


def test_old_file_with_no_is_test_key_defaults_to_production(tmp_path):
    """A session.json written before the is_test field existed (round-1 fix
    for task 11's critical defect) has no "is_test" key at all --
    SessionSnapshot(**raw) must fill it in as False (production), the
    fail-safe default, rather than raising or silently treating an old real
    pending sale as a test sale that must never reach PAY-104 recovery."""
    path = tmp_path / "session.json"
    old_style = {
        "state": "dispensing",
        "credit_escrow_cents": 0,
        "selected_sku": "ICE-1",
        "dispense_slot": 2,
        "dispense_started_at": 123.0,
        "credits": [],
        "pending_sale_shares": {"cash_bill": 250},
        "saved_at": 100.0,
        "error": None,
    }
    path.write_text(json.dumps(old_style), encoding="utf-8")

    loaded = SessionStore(path).load()

    assert loaded.error is None
    assert loaded.is_test is False
    assert loaded.pending_sale_shares == {"cash_bill": 250}
    assert loaded.is_open() is True


def test_old_file_with_no_dispense_mechanism_key_still_loads(tmp_path):
    """A session.json written before dispense_mechanism existed (plan:
    dispenser profiles, Task 3) has no such key at all -- SessionSnapshot(
    **raw) must fill it in as None rather than raising."""
    path = tmp_path / "session.json"
    old_style = {
        "state": "dispensing",
        "credit_escrow_cents": 0,
        "selected_sku": "ICE-1",
        "dispense_slot": 2,
        "dispense_started_at": 123.0,
        "credits": [],
        "pending_sale_shares": {"cash_bill": 2.50},
        "is_test": False,
        "saved_at": 100.0,
        "error": None,
    }
    path.write_text(json.dumps(old_style), encoding="utf-8")

    loaded = SessionStore(path).load()

    assert loaded.error is None
    assert loaded.dispense_mechanism is None
    assert loaded.is_open() is True


def test_dispense_mechanism_round_trips(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=0,
        selected_sku="ICE-1",
        dispense_slot=2,
        dispense_mechanism="bagged_ice",
        dispense_started_at=123.0,
    )
    store.save(snap)
    loaded = store.load()
    assert loaded == snap
    assert loaded.dispense_mechanism == "bagged_ice"


def test_load_missing_returns_none(tmp_path):
    assert SessionStore(tmp_path / "session.json").load() is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionSnapshot(state="idle", credit_escrow_cents=100))
    store.clear()
    store.clear()
    assert store.load() is None


def test_clear_returns_true_on_missing_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    assert store.clear() is True


def test_clear_returns_false_when_unlink_raises(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionSnapshot(state="idle", credit_escrow_cents=100))

    def boom(self, missing_ok=False):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", boom)
    assert store.clear() is False


def test_corrupt_file_is_an_open_session_with_error(tmp_path):
    p = tmp_path / "session.json"
    p.write_text("{not json", encoding="utf-8")
    snap = SessionStore(p).load()
    assert snap is not None
    assert snap.error
    assert snap.is_open() is True


def test_is_open_rules():
    assert SessionSnapshot(state="idle", credit_escrow_cents=0).is_open() is False
    assert (
        SessionSnapshot(state="interacting_with_user", credit_escrow_cents=25).is_open()
        is True
    )
    assert SessionSnapshot(state="dispensing", credit_escrow_cents=0).is_open() is True


async def test_save_async_writes_in_order(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    await store.save_async(SessionSnapshot(state="idle", credit_escrow_cents=100))
    await store.save_async(SessionSnapshot(state="idle", credit_escrow_cents=200))
    assert (
        json.loads((tmp_path / "session.json").read_text())["credit_escrow_cents"]
        == 200
    )
    await store.clear_async()
    assert store.load() is None


def test_saved_at_survives_round_trip_across_a_process_boundary(tmp_path):
    """PAY-104 recovery (ice_edge/sale/vmc.py's _pay104_sale_key) keys on the
    exact saved_at value read back from disk being the same instant that was
    written -- not a value rewritten in place on reload. Reload with a
    freshly constructed SessionStore (not the one that wrote it) to prove
    this holds across a process boundary, not just in the same instance."""
    path = tmp_path / "session.json"
    writer = SessionStore(path)
    snap = SessionSnapshot(
        state="interacting_with_user", credit_escrow_cents=125, saved_at=1_700_000_000.5
    )
    writer.save(snap)

    reader = SessionStore(path)
    loaded = reader.load()

    assert loaded.saved_at == 1_700_000_000.5


def test_missing_saved_at_is_reported_as_unreadable_not_defaulted_to_now(tmp_path):
    """A snapshot file written without a saved_at key must not silently pick
    up field(default_factory=time.time) as if it were saved just now -- that
    would hand PAY-104 recovery a fabricated timestamp for real, on-disk
    credit. It must instead route through the same 'error' channel as an
    unparseable file, so credit_escrow from the file is NOT trusted either."""
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({"state": "dispensing", "credit_escrow_cents": 500}),
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow_cents == 0  # the fabricated 5.0 was not trusted
    assert loaded.is_open() is True


def test_non_finite_saved_at_is_reported_as_unreadable(tmp_path):
    """NaN/Infinity are valid JSON under Python's parser but are not a
    sensible 'moment' for PAY-104 recovery to reason about; they must be
    detected, not accepted as-is."""
    path = tmp_path / "session.json"
    path.write_text(
        '{"state": "dispensing", "credit_escrow_cents": 500, "saved_at": NaN}',
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow_cents == 0
    assert loaded.is_open() is True


def test_negative_saved_at_is_reported_as_unreadable(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(
        '{"state": "dispensing", "credit_escrow_cents": 500, "saved_at": -1.0}',
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow_cents == 0
    assert loaded.is_open() is True


def test_is_open_unchanged_for_open_and_cleared_snapshot_after_guard(tmp_path):
    """The saved_at guard must not change is_open()'s semantics: a snapshot
    with real credit in escrow is still open, and an idle/no-credit snapshot
    is still not, once round-tripped through save/load."""
    store = SessionStore(tmp_path / "session.json")

    open_snap = SessionSnapshot(
        state="interacting_with_user", credit_escrow_cents=75, saved_at=100.0
    )
    store.save(open_snap)
    assert store.load().is_open() is True

    cleared_snap = SessionSnapshot(state="idle", credit_escrow_cents=0, saved_at=200.0)
    store.save(cleared_snap)
    assert store.load().is_open() is False


def test_future_saved_at_beyond_skew_loads_with_a_warning(tmp_path):
    """A Pi with no RTC can boot with its clock behind the last save. The
    saved_at is the PAY-104 recovery key, so it is kept as-is and the snapshot
    still loads with its details; only a warning names the skew."""
    path = tmp_path / "session.json"
    future = time.time() + SAVED_AT_FUTURE_SKEW_SECONDS + 3600.0
    path.write_text(
        json.dumps(
            {"state": "dispensing", "credit_escrow_cents": 500, "saved_at": future}
        ),
        encoding="utf-8",
    )
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        loaded = SessionStore(path).load()
    finally:
        logger.remove(sink)

    assert loaded is not None
    assert loaded.error is None
    assert loaded.credit_escrow_cents == 500
    assert loaded.saved_at == future
    assert loaded.is_open() is True
    assert any("saved_at" in m and "ahead" in m for m in messages)


def test_saved_at_just_within_future_skew_is_accepted(tmp_path):
    """The allowed skew must not be so tight that an ordinary write/read
    round trip (or a slightly fast system clock) gets rejected -- a
    saved_at a little ahead of "now" but still inside the tolerance is a
    valid snapshot, not a corrupt one."""
    path = tmp_path / "session.json"
    within_skew = time.time() + (SAVED_AT_FUTURE_SKEW_SECONDS / 2)
    path.write_text(
        json.dumps(
            {"state": "dispensing", "credit_escrow_cents": 500, "saved_at": within_skew}
        ),
        encoding="utf-8",
    )

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        loaded = SessionStore(path).load()
    finally:
        logger.remove(sink)

    assert loaded is not None
    assert loaded.error is None
    assert loaded.credit_escrow_cents == 500
    assert loaded.saved_at == within_skew
    assert not any("saved_at" in m for m in messages)


def test_zero_saved_at_is_reported_as_unreadable(tmp_path):
    """The brief's rationale lists a saved_at of zero alongside missing,
    future, and silently-re-defaulted values as corrupt. The Unix epoch is
    not a plausible "moment a live sale's escrow was written" on a machine
    whose clock has ever been set, so it is rejected the same way a
    negative value is -- not treated as a legitimate (if odd) timestamp."""
    path = tmp_path / "session.json"
    path.write_text(
        '{"state": "dispensing", "credit_escrow_cents": 500, "saved_at": 0.0}',
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow_cents == 0
    assert loaded.is_open() is True


def test_save_writes_units_cents_marker(tmp_path):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(state="interacting_with_user", credit_escrow_cents=125)
    )

    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["units"] == "cents"
    assert raw["credit_escrow_cents"] == 125


def test_cents_file_is_not_reconverted_on_load(tmp_path):
    """A file carrying units=cents is read as-is; 125 must not become 12500."""
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow_cents=125,
        credits=[Credit(method="cash_bill", cents=125, ts=1.0)],
        pending_sale_shares={"cash": 250},
    )
    store.save(snap)

    assert store.load() == snap


def test_credit_event_id_round_trips_and_old_credit_loads_without_it(tmp_path):
    path = tmp_path / "session.json"
    store = SessionStore(path)
    store.save(
        SessionSnapshot(
            state="idle",
            credit_escrow_cents=300,
            credits=[Credit("cash_bill", 300, 1.0, event_id="evt-boot-0001-0007")],
        )
    )
    assert store.load().credits[0].event_id == "evt-boot-0001-0007"

    raw = json.loads(path.read_text())
    del raw["credits"][0]["event_id"]  # a file from before the field existed
    path.write_text(json.dumps(raw))
    assert store.load().credits[0].event_id is None


def test_pending_payouts_keep_a_snapshot_open_and_round_trip(tmp_path):
    payout = {
        "request_id": "r" * 8,
        "kind": "payout",
        "cents": 200,
        "reason": "customer",
        "sale_id": None,
    }
    snap = SessionSnapshot(
        state="idle", credit_escrow_cents=0, pending_payouts=[payout]
    )
    assert snap.is_open()
    assert not SessionSnapshot(state="idle", credit_escrow_cents=0).is_open()

    store = SessionStore(tmp_path / "session.json")
    store.save(snap)
    assert store.load().pending_payouts == [payout]


def test_pre_cents_file_loads_as_an_open_error_snapshot(tmp_path):
    """Legacy dollar / refund-id keys are no longer understood; the file must
    fail safe as an unreadable (open, PAY-104) snapshot, never as 'closed'."""
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "state": "idle",
                "credit_escrow": 1.25,
                "pending_refund_request_id": "r" * 8,
                "saved_at": 100.0,
            }
        ),
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded.error
    assert loaded.is_open()
