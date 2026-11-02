"""atomic_write: the one tmp + fsync + (bak) + replace + dir-fsync helper."""

import os

import pytest

from ice_common import paths
from ice_common.paths import atomic_write


def test_bytes_round_trip(tmp_path):
    target = tmp_path / "a.bin"
    atomic_write(target, b"\x00\x01\r\n")
    assert target.read_bytes() == b"\x00\x01\r\n"


def test_str_round_trip_with_encoding(tmp_path):
    target = tmp_path / "a.txt"
    atomic_write(target, "café", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "café"
    atomic_write(target, "café", encoding="latin-1")
    assert target.read_bytes() == b"caf\xe9"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_mode_is_applied_under_permissive_umask(tmp_path):
    target = tmp_path / "secret.json"
    (tmp_path / "secret.json.tmp").write_text("stale")
    os.chmod(tmp_path / "secret.json.tmp", 0o644)
    previous = os.umask(0o000)
    try:
        atomic_write(target, "{}", mode=0o600, backup=True)
        atomic_write(target, "{}", mode=0o600, backup=True)
    finally:
        os.umask(previous)
    assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "secret.json.bak").stat().st_mode & 0o777 == 0o600


def test_backup_keeps_previous_content(tmp_path):
    target = tmp_path / "config.json"
    atomic_write(target, "one", backup=True)
    assert not (tmp_path / "config.json.bak").exists()
    atomic_write(target, "two", backup=True)
    assert target.read_text() == "two"
    assert (tmp_path / "config.json.bak").read_text() == "one"


def test_no_backup_by_default(tmp_path):
    target = tmp_path / "x.json"
    atomic_write(target, "one")
    atomic_write(target, "two")
    assert not (tmp_path / "x.json.bak").exists()


def test_replace_failure_leaves_target_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    atomic_write(target, "old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, "new", backup=True)
    assert target.read_text() == "old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_fsync_file_before_replace_then_directory(tmp_path, monkeypatch):
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        paths.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        paths.os,
        "replace",
        lambda s, d: (calls.append("replace"), real_replace(s, d))[1],
    )
    seen_dirs: list[object] = []
    monkeypatch.setattr(paths, "fsync_dir", seen_dirs.append)

    atomic_write(tmp_path / "x.json", "data")

    assert calls == ["fsync", "replace"]
    assert seen_dirs == [tmp_path]
