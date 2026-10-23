"""Machine identity label shared by the machine and the cloud portal."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MachineLabel:
    """How a machine names itself: common name, machine id, short fingerprint."""

    common_name: str | None = None
    machine_id: str | None = None
    short_fingerprint: str | None = None

    @property
    def display(self) -> str:
        if self.common_name and self.machine_id:
            return f"{self.common_name} ({self.machine_id})"
        return self.common_name or self.machine_id or self.short_fingerprint or ""
