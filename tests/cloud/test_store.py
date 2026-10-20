import pytest

from ice_cloud.store import SnapshotStore, check_fingerprint
from tests.cloud.reports import BOOT_A, BOOT_B, FP, FP_2, make_report


def test_save_and_latest_round_trip(tmp_path):
    store = SnapshotStore(tmp_path)
    report = make_report()
    assert store.save(FP, report) is True
    assert store.latest(FP) == report
    assert store.latest(FP_2) is None


def test_older_or_equal_seq_from_same_boot_is_ignored(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(FP, make_report(seq=5))
    assert store.save(FP, make_report(seq=4)) is False
    assert store.save(FP, make_report(seq=5)) is False
    assert store.latest(FP).seq == 5
    assert store.save(FP, make_report(seq=6)) is True
    assert store.latest(FP).seq == 6


def test_new_boot_id_replaces_even_with_seq_zero(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(FP, make_report(seq=50, boot_id=BOOT_A))
    assert store.save(FP, make_report(seq=0, boot_id=BOOT_B)) is True
    assert store.latest(FP).boot_id == BOOT_B


@pytest.mark.parametrize(
    "bad",
    ["../x", "a/b", "", "x" * 65, "sp ace", "a\\b", "m-0042", "AB" * 32, "ab" * 31],
)
def test_bad_fingerprint_raises(tmp_path, bad):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError):
        store.latest(bad)


def test_no_tmp_files_remain(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(FP, make_report(seq=1))
    store.save(FP, make_report(seq=2))
    assert list(tmp_path.rglob("*.tmp")) == []
    assert [p.name for p in (tmp_path / "machines").iterdir()] == [f"{FP}.json"]


def test_machines_sorted_by_label_with_fingerprint_and_corrupt_file_skipped(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(FP, make_report(machine_id="b-2"))
    store.save(FP_2, make_report(machine_id="a-1"))
    corrupt = "ef" * 32
    (tmp_path / "machines" / f"{corrupt}.json").write_text(
        "{not json", encoding="utf-8"
    )
    (tmp_path / "machines" / "stray.json").write_text("{}", encoding="utf-8")
    assert [(fp, r.machine_id) for fp, r in store.machines()] == [
        (FP_2, "a-1"),
        (FP, "b-2"),
    ]
    assert store.latest(corrupt) is None


def test_check_fingerprint_returns_valid_value():
    assert check_fingerprint(FP) == FP
