import json
from pathlib import Path

from services.session_store import SessionSnapshot, SessionStore


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
