"""Back up SQLite databases and loose files into one verified tar.gz archive.

Standard library only: it runs inside the slim edge and cloud images and must
not import any ``ice_*`` package.

    backup_data.py --dest DIR [--keep N] [--min-age-hours H] [--sqlite FILE ...] [--file PATH ...] [--label NAME]
    backup_data.py --verify ARCHIVE

A database is copied with the sqlite3 online backup API (a plain file copy of
a WAL-mode database can be inconsistent), then checked with
``PRAGMA integrity_check``. Only the paths named on the command line are
archived; nothing is discovered.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_KEEP = 14
MANIFEST = "MANIFEST.json"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity_ok(db_path: Path) -> bool:
    """True when ``PRAGMA integrity_check`` on a read-only open returns ok."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return rows == [("ok",)]


def snapshot_sqlite(source: Path, target: Path) -> None:
    """Write a consistent copy of ``source`` to ``target``; raise if it is not sound."""
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if not integrity_ok(target):
        raise RuntimeError(f"integrity_check failed for {source}")


def build_archive(
    dest: Path,
    label: str,
    sqlite_files: list[Path],
    files: list[Path],
    now: datetime | None = None,
) -> Path:
    """Create ``dest/<label>-<UTC stamp>.tar.gz`` atomically and return its path."""
    now = now or datetime.now(UTC)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = dest / f"{label}-{now.strftime(STAMP_FORMAT)}.tar.gz"
    with tempfile.TemporaryDirectory(dir=dest, prefix=".backup-") as work_name:
        work = Path(work_name)
        members: list[Path] = []
        for db in sqlite_files:
            if not db.exists():
                raise FileNotFoundError(f"sqlite database not found: {db}")
            snap = work / db.name
            snapshot_sqlite(db, snap)
            log(f"snapshot {db} ({snap.stat().st_size} bytes)")
            members.append(snap)
        for path in files:
            if not path.exists():
                log(f"skip {path}: does not exist")
                continue
            # Copy first, then hash and archive the copy, so the manifest and
            # the archived bytes cannot disagree if the source is rewritten.
            copy = work / path.name
            if copy.exists():
                raise ValueError(f"duplicate member names: {path.name}")
            shutil.copyfile(path, copy)
            members.append(copy)
            log(f"include {path}")
        manifest = {
            "created": now.strftime(STAMP_FORMAT),
            "label": label,
            "members": [
                {"name": m.name, "size": m.stat().st_size, "sha256": sha256_of(m)}
                for m in members
            ],
        }
        names = [m["name"] for m in manifest["members"]]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate member names: {names}")
        tmp_archive = work / "archive.tar.gz"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(data)
            info.mtime = int(now.timestamp())
            tar.addfile(info, io.BytesIO(data))
            for member in members:
                tar.add(member, arcname=member.name)
        os.chmod(tmp_archive, 0o600)
        tmp_archive.replace(final)
    log(f"wrote {final}")
    return final


def _archive_pattern(label: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(label)}-\d{{8}}T\d{{6}}Z\.tar\.gz$")


def newest_age_hours(dest: Path, label: str, now: float | None = None) -> float | None:
    """Age in hours of the newest archive of ``label`` in ``dest`` (by mtime), or None."""
    if not dest.is_dir():
        return None
    pattern = _archive_pattern(label)
    mtimes = [p.stat().st_mtime for p in dest.iterdir() if pattern.match(p.name)]
    if not mtimes:
        return None
    return ((now if now is not None else time.time()) - max(mtimes)) / 3600


def remove_stale_workdirs(
    dest: Path, max_age_seconds: float = 3600, now: float | None = None
) -> None:
    """Delete ``.backup-*`` work dirs older than an hour (left by a kill mid-run)."""
    if not dest.is_dir():
        return
    cutoff = (now if now is not None else time.time()) - max_age_seconds
    for path in dest.iterdir():
        if (
            path.is_dir()
            and path.name.startswith(".backup-")
            and path.stat().st_mtime < cutoff
        ):
            shutil.rmtree(path, ignore_errors=True)
            log(f"removed stale work dir {path}")


def prune(dest: Path, label: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` archives of ``label``; return those removed."""
    pattern = _archive_pattern(label)
    archives = sorted(p for p in dest.iterdir() if pattern.match(p.name))
    removed = archives[: max(len(archives) - keep, 0)]
    for old in removed:
        old.unlink(missing_ok=True)
        log(f"pruned {old}")
    return removed


def verify(archive: Path) -> bool:
    """Check every member against the manifest and integrity_check any .db member."""
    ok = True
    with tempfile.TemporaryDirectory(
        dir=archive.parent, prefix=".backup-verify-"
    ) as work_name:
        work = Path(work_name)
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            log(f"members: {', '.join(names)}")
            if MANIFEST not in names:
                log(f"FAIL {MANIFEST} missing")
                return False
            manifest = json.load(tar.extractfile(MANIFEST))
            for entry in manifest["members"]:
                name = entry["name"]
                if (
                    not isinstance(name, str)
                    or not name
                    or Path(name).name != name
                    or name in (".", "..")
                    or "/" in name
                    or "\\" in name
                ):
                    log(f"FAIL unsafe member name {name!r}")
                    return False
                if name not in names:
                    log(f"FAIL {name}: missing from archive")
                    ok = False
                    continue
                target = work / name
                target.write_bytes(tar.extractfile(name).read())
                if sha256_of(target) != entry["sha256"]:
                    log(f"FAIL {name}: sha256 mismatch")
                    ok = False
                    continue
                if name.endswith(".db") and not integrity_ok(target):
                    log(f"FAIL {name}: integrity_check")
                    ok = False
                    continue
                log(f"ok {name}")
    return ok


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, help="directory that receives the archive")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--sqlite", type=Path, action="append", default=[])
    parser.add_argument("--file", type=Path, action="append", default=[], dest="files")
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=0,
        help="skip the run when the newest archive is younger than this (0 = always run)",
    )
    parser.add_argument("--label", default="backup")
    parser.add_argument("--verify", type=Path, metavar="ARCHIVE")
    args = parser.parse_args(argv)
    if args.verify is None and args.dest is None:
        parser.error("--dest is required unless --verify is given")
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    try:
        if args.verify is not None:
            return 0 if verify(args.verify) else 1
        remove_stale_workdirs(args.dest)
        if args.min_age_hours > 0:
            age = newest_age_hours(args.dest, args.label)
            if age is not None and 0 <= age < args.min_age_hours:
                log(
                    f"skip: newest {args.label} archive is {age:.1f} h old (< {args.min_age_hours} h)"
                )
                return 0
        build_archive(args.dest, args.label, args.sqlite, args.files)
        prune(args.dest, args.label, args.keep)
    except Exception as exc:
        log(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
