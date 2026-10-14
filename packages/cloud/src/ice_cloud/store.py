"""Latest-report-per-machine store (cloud spec section 4).

One JSON file per machine under ``<root>/machines/``. A report with the same
``boot_id`` and a ``seq`` no greater than the stored one is ignored; a new
``boot_id`` always replaces.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from ice_contracts.cloud.report import MachineReport

MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_machine_id(machine_id: str) -> str:
    if not MACHINE_ID_RE.fullmatch(machine_id):
        raise ValueError(f"invalid machine_id: {machine_id!r}")
    return machine_id


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self._dir = Path(root) / "machines"
        self._lock = threading.Lock()

    def _path(self, machine_id: str) -> Path:
        return self._dir / f"{check_machine_id(machine_id)}.json"

    def latest(self, machine_id: str) -> MachineReport | None:
        path = self._path(machine_id)
        try:
            return MachineReport.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None

    def machines(self) -> list[MachineReport]:
        if not self._dir.is_dir():
            return []
        found = []
        for path in self._dir.glob("*.json"):
            if not MACHINE_ID_RE.fullmatch(path.stem):
                continue
            report = self.latest(path.stem)
            if report is not None:
                found.append(report)
        return sorted(found, key=lambda r: r.machine_id)

    def save(self, report: MachineReport) -> bool:
        """Store *report*; ``False`` (stored report untouched) when it is not
        newer than the stored one of the same boot."""
        path = self._path(report.machine_id)
        with self._lock:
            stored = self.latest(report.machine_id)
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
