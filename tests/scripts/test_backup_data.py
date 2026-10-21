"""Tests for scripts/backup_data.py (stdlib backup/verify tool)."""

import hashlib
import importlib.util
import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_data.py"

spec = importlib.util.spec_from_file_location("backup_data", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def _wal_db(path: Path, rows: int = 5) -> sqlite3.Connection:
    """A WAL-mode DB with uncheckpointed writes; the returned connection keeps the WAL alive."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, sku TEXT)")
    conn.executemany(
        "INSERT INTO sales (sku) VALUES (?)", [(f"sku{i}",) for i in range(rows)]
    )
    conn.commit()
    return conn


def _read(archive: Path, name: str) -> bytes:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.extractfile(name).read()


def _names(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def test_snapshot_contains_uncheckpointed_wal_rows(tmp_path):
    db = tmp_path / "events.db"
    conn = _wal_db(db)
    try:
        # The rows live only in the -wal file, so a plain copy of the db would miss them.
        assert Path(str(db) + "-wal").stat().st_size > 0
        assert (
            backup.main(
                [
                    "--dest",
                    str(tmp_path / "out"),
                    "--label",
                    "edge",
                    "--sqlite",
                    str(db),
                ]
            )
            == 0
        )
    finally:
        conn.close()
    (archive,) = (tmp_path / "out").glob("edge-*.tar.gz")
    restored = tmp_path / "restored.db"
    restored.write_bytes(_read(archive, "events.db"))
    check = sqlite3.connect(restored)
    try:
        assert check.execute("SELECT count(*) FROM sales").fetchone() == (5,)
    finally:
        check.close()


def test_files_included_missing_skipped_and_manifest_matches(tmp_path, capsys):
    present = tmp_path / "access.json"
    present.write_bytes(b'{"users": []}\n')
    missing = tmp_path / "cloud_identity.json"
    code = backup.main(
        [
            "--dest",
            str(tmp_path / "out"),
            "--file",
            str(present),
            "--file",
            str(missing),
        ]
    )
    assert code == 0
    assert "skip" in capsys.readouterr().out
    (archive,) = (tmp_path / "out").glob("backup-*.tar.gz")
    assert sorted(_names(archive)) == ["MANIFEST.json", "access.json"]
    assert _read(archive, "access.json") == present.read_bytes()
    manifest = json.loads(_read(archive, "MANIFEST.json"))
    assert manifest["label"] == "backup"
    assert manifest["members"] == [
        {
            "name": "access.json",
            "size": len(present.read_bytes()),
            "sha256": hashlib.sha256(present.read_bytes()).hexdigest(),
        }
    ]


def test_unnamed_files_are_not_archived(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "access.json").write_text("{}")
    (data / "session.json").write_text('{"state": "dispensing"}')
    assert (
        backup.main(
            ["--dest", str(tmp_path / "out"), "--file", str(data / "access.json")]
        )
        == 0
    )
    (archive,) = (tmp_path / "out").glob("backup-*.tar.gz")
    assert "session.json" not in _names(archive)


def test_prune_keeps_newest_for_label_only(tmp_path):
    dest = tmp_path / "out"
    source = tmp_path / "f.json"
    source.write_text("{}")
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for day in range(5):
        backup.build_archive(dest, "edge", [], [source], now=base + timedelta(days=day))
    backup.build_archive(dest, "cloud", [], [source], now=base)
    removed = backup.prune(dest, "edge", keep=2)
    assert len(removed) == 3
    assert sorted(p.name for p in dest.glob("edge-*")) == [
        "edge-20261004T000000Z.tar.gz",
        "edge-20261005T000000Z.tar.gz",
    ]
    assert len(list(dest.glob("cloud-*"))) == 1


def test_verify_passes_good_archive_and_fails_corrupted_member(tmp_path):
    db = tmp_path / "cloud.db"
    conn = _wal_db(db)
    conn.close()
    archive = backup.build_archive(tmp_path / "out", "cloud", [db], [])
    assert backup.main(["--verify", str(archive)]) == 0

    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(bad, "w:gz") as dst:
        for member in src.getmembers():
            payload = src.extractfile(member).read()
            if member.name == "cloud.db":
                payload = payload[:-1] + bytes([payload[-1] ^ 0xFF])
            member.size = len(payload)
            dst.addfile(member, io.BytesIO(payload))
    assert backup.main(["--verify", str(bad)]) == 1


def test_corrupt_source_db_fails_and_writes_no_archive(tmp_path):
    db = tmp_path / "events.db"
    conn = _wal_db(db, rows=2000)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = bytearray(db.read_bytes())
    for offset in range(
        4096, len(raw), 512
    ):  # trash table pages, keep the header valid
        raw[offset : offset + 64] = b"\xff" * 64
    db.write_bytes(bytes(raw))
    dest = tmp_path / "out"
    assert backup.main(["--dest", str(dest), "--sqlite", str(db)]) == 1
    assert not list(dest.glob("*.tar.gz"))
