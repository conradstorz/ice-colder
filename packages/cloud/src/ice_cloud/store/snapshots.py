"""Latest-report-per-machine store (cloud spec section 4).

Keyed by machine fingerprint (64 lower-case hex). One JSON file per machine
under ``<root>/machines/``. A report with the same ``boot_id`` and a ``seq`` no
greater than the stored one is ignored; a new ``boot_id`` always replaces.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from ice_contracts.cloud.report import MachineReport

FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def check_fingerprint(fingerprint: str) -> str:
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError(f"invalid fingerprint: {fingerprint!r}")
    return fingerprint


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self._dir = Path(root) / "machines"
        self._lock = threading.Lock()

    def _path(self, fingerprint: str) -> Path:
        return self._dir / f"{check_fingerprint(fingerprint)}.json"

    def latest(self, fingerprint: str) -> MachineReport | None:
        path = self._path(fingerprint)
        try:
            return MachineReport.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None

    def machines(self) -> list[tuple[str, MachineReport]]:
        """``(fingerprint, report)`` pairs, sorted by the report's label."""
        if not self._dir.is_dir():
            return []
        found = []
        for path in self._dir.glob("*.json"):
            if not FINGERPRINT_RE.fullmatch(path.stem):
                continue
            report = self.latest(path.stem)
            if report is not None:
                found.append((path.stem, report))
        return sorted(found, key=lambda pair: (pair[1].machine_id, pair[0]))

    def save(self, fingerprint: str, report: MachineReport) -> bool:
        """Store *report*; ``False`` (stored report untouched) when it is not
        newer than the stored one of the same boot."""
        path = self._path(fingerprint)
        with self._lock:
            stored = self.latest(fingerprint)
            if (
                stored is not None
                and stored.boot_id == report.boot_id
                and stored.seq >= report.seq
            ):
                return False
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self._dir, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(report.model_dump_json().encode("utf-8"))
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return True
