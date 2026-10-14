import pytest

from ice_cloud.store import SnapshotStore
from tests.cloud.reports import BOOT_A, BOOT_B, make_report


def test_save_and_latest_round_trip(tmp_path):
    store = SnapshotStore(tmp_path)
    report = make_report()
    assert store.save(report) is True
    assert store.latest("m-0042") == report
    assert store.latest("m-9999") is None


def test_older_or_equal_seq_from_same_boot_is_ignored(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(make_report(seq=5))
    assert store.save(make_report(seq=4)) is False
    assert store.save(make_report(seq=5)) is False
    assert store.latest("m-0042").seq == 5
    assert store.save(make_report(seq=6)) is True
    assert store.latest("m-0042").seq == 6


def test_new_boot_id_replaces_even_with_seq_zero(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(make_report(seq=50, boot_id=BOOT_A))
    assert store.save(make_report(seq=0, boot_id=BOOT_B)) is True
    assert store.latest("m-0042").boot_id == BOOT_B


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 65, "sp ace", "a\\b"])
def test_bad_machine_id_raises(tmp_path, bad):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError):
        store.latest(bad)


def test_no_tmp_files_remain(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(make_report(seq=1))
    store.save(make_report(seq=2))
    assert list(tmp_path.rglob("*.tmp")) == []
    assert [p.name for p in (tmp_path / "machines").iterdir()] == ["m-0042.json"]


def test_machines_sorted_by_id_and_corrupt_file_is_skipped(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(make_report(machine_id="b-2"))
    store.save(make_report(machine_id="a-1"))
    (tmp_path / "machines" / "c-3.json").write_text("{not json", encoding="utf-8")
    assert [r.machine_id for r in store.machines()] == ["a-1", "b-2"]
    assert store.latest("c-3") is None
