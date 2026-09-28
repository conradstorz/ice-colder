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
