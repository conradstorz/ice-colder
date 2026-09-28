import json
from pathlib import Path

from services.session_store import Credit, SessionSnapshot, SessionStore


def test_round_trip(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(
        state="dispensing",
        credit_escrow=0.0,
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
        credit_escrow=0.50,
        selected_sku="ICE-1",
        dispense_slot=2,
        dispense_started_at=123.0,
        credits=[
            Credit(method="cash_bill", amount=2.00, ts=10.0),
            Credit(method="card", amount=0.50, ts=20.0),
        ],
        pending_sale_shares={"cash_bill": 2.00, "card": 0.50},
    )

    store.save(snap)
    loaded = store.load()

    assert loaded == snap
    assert all(isinstance(c, Credit) for c in loaded.credits)
    assert loaded.credits[0].method == "cash_bill"
    assert loaded.credits[1].amount == 0.50
    assert loaded.pending_sale_shares == {"cash_bill": 2.00, "card": 0.50}


def test_round_trip_with_empty_credits_and_none_shares(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    snap = SessionSnapshot(state="idle", credit_escrow=0.0)

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
        "credit_escrow": 1.25,
        "selected_sku": "ICE-1",
        "dispense_slot": None,
        "dispense_started_at": None,
        "pending_refund_request_id": None,
        "saved_at": 100.0,
        "error": None,
    }
    path.write_text(json.dumps(old_style), encoding="utf-8")

    loaded = SessionStore(path).load()

    assert loaded.error is None
    assert loaded.credit_escrow == 1.25
    assert loaded.credits == []
    assert loaded.pending_sale_shares is None


def test_load_missing_returns_none(tmp_path):
    assert SessionStore(tmp_path / "session.json").load() is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionSnapshot(state="idle", credit_escrow=1.0))
    store.clear()
    store.clear()
    assert store.load() is None


def test_clear_returns_true_on_missing_file(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    assert store.clear() is True


def test_clear_returns_false_when_unlink_raises(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session.json")
    store.save(SessionSnapshot(state="idle", credit_escrow=1.0))

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
    assert SessionSnapshot(state="idle", credit_escrow=0.0).is_open() is False
    assert (
        SessionSnapshot(state="interacting_with_user", credit_escrow=0.25).is_open()
        is True
    )
    assert SessionSnapshot(state="dispensing", credit_escrow=0.0).is_open() is True
    assert (
        SessionSnapshot(
            state="idle", credit_escrow=0.0, pending_refund_request_id="abc"
        ).is_open()
        is True
    )


async def test_save_async_writes_in_order(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    await store.save_async(SessionSnapshot(state="idle", credit_escrow=1.0))
    await store.save_async(SessionSnapshot(state="idle", credit_escrow=2.0))
    assert json.loads((tmp_path / "session.json").read_text())["credit_escrow"] == 2.0
    await store.clear_async()
    assert store.load() is None


def test_saved_at_survives_round_trip_across_a_process_boundary(tmp_path):
    """PAY-104 recovery (controller/vmc.py's _pay104_sale_key) keys on the
    exact saved_at value read back from disk being the same instant that was
    written -- not a value rewritten in place on reload. Reload with a
    freshly constructed SessionStore (not the one that wrote it) to prove
    this holds across a process boundary, not just in the same instance."""
    path = tmp_path / "session.json"
    writer = SessionStore(path)
    snap = SessionSnapshot(
        state="interacting_with_user", credit_escrow=1.25, saved_at=1_700_000_000.5
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
        json.dumps({"state": "dispensing", "credit_escrow": 5.0}), encoding="utf-8"
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow == 0.0  # the fabricated 5.0 was not trusted
    assert loaded.is_open() is True


def test_non_finite_saved_at_is_reported_as_unreadable(tmp_path):
    """NaN/Infinity are valid JSON under Python's parser but are not a
    sensible 'moment' for PAY-104 recovery to reason about; they must be
    detected, not accepted as-is."""
    path = tmp_path / "session.json"
    path.write_text(
        '{"state": "dispensing", "credit_escrow": 5.0, "saved_at": NaN}',
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow == 0.0
    assert loaded.is_open() is True


def test_negative_saved_at_is_reported_as_unreadable(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(
        '{"state": "dispensing", "credit_escrow": 5.0, "saved_at": -1.0}',
        encoding="utf-8",
    )

    loaded = SessionStore(path).load()

    assert loaded is not None
    assert loaded.error is not None
    assert loaded.credit_escrow == 0.0
    assert loaded.is_open() is True


def test_is_open_unchanged_for_open_and_cleared_snapshot_after_guard(tmp_path):
    """The saved_at guard must not change is_open()'s semantics: a snapshot
    with real credit in escrow is still open, and an idle/no-credit snapshot
    is still not, once round-tripped through save/load."""
    store = SessionStore(tmp_path / "session.json")

    open_snap = SessionSnapshot(
        state="interacting_with_user", credit_escrow=0.75, saved_at=100.0
    )
    store.save(open_snap)
    assert store.load().is_open() is True

    cleared_snap = SessionSnapshot(state="idle", credit_escrow=0.0, saved_at=200.0)
    store.save(cleared_snap)
    assert store.load().is_open() is False
