import json
import os

import pytest

from ice_edge.ledger.payment_cursor import CursorState, PaymentCursorStore


def test_missing_file_gives_empty_state(tmp_path):
    st = PaymentCursorStore(tmp_path / "c.json").load()
    assert st.seqs == {} and not st.recent_ids and not st.corrupt


def test_round_trip_keeps_two_boots_and_ids(tmp_path):
    store = PaymentCursorStore(tmp_path / "c.json")
    st = CursorState()
    st.advance("boot-aaaa", 1, "evt-00001")
    st.advance("boot-bbbb", 4, "evt-00002")
    st.advance("boot-cccc", 2, "evt-00003")
    store.save(st)
    got = store.load()
    assert got.seqs == {"boot-bbbb": 4, "boot-cccc": 2}
    assert list(got.recent_ids) == ["evt-00001", "evt-00002", "evt-00003"]
    assert got.recent_ids.maxlen == 64


def test_recent_ids_capped_at_64():
    st = CursorState()
    for i in range(70):
        st.advance("boot-aaaa", i + 1, f"evt-{i:05d}")
    assert len(st.recent_ids) == 64
    assert st.recent_ids[0] == "evt-00006"


def test_corrupt_file_renamed_aside(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    st = PaymentCursorStore(p).load()
    assert st.corrupt and st.seqs == {}
    assert not p.exists()
    assert list(tmp_path.glob("c.json.corrupt-*"))


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    store = PaymentCursorStore(p)
    st = CursorState()
    st.advance("boot-aaaa", 1, "evt-00001")
    store.save(st)
    before = p.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", boom)
    st.advance("boot-aaaa", 2, "evt-00002")
    with pytest.raises(OSError):
        store.save(st)
    assert p.read_text(encoding="utf-8") == before
    assert os.path.exists(p)


async def test_save_async(tmp_path):
    store = PaymentCursorStore(tmp_path / "c.json")
    st = CursorState()
    st.advance("boot-aaaa", 3, "evt-00001")
    await store.save_async(st)
    assert store.load().seqs == {"boot-aaaa": 3}
