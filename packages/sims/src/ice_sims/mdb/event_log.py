"""Board-side replay log for `payment/event` envelopes."""

import copy
import secrets
import string
from datetime import datetime

MAX_UNACKED = 256  # capacity of the board's flash log; never reached (see pressure)
# Backpressure, as `EventLog::next_pressure` in firmware/mdb/lib/mdbcore/event_log.h:
# at PRESSURE_SET pending the board inhibits acceptance (`device_fault`); it
# resumes once pending falls to PRESSURE_CLEAR. Unacked events are never evicted.
PRESSURE_SET = 192
PRESSURE_CLEAR = 128
_ALPHABET = string.ascii_lowercase + string.digits


class EventLog:
    def __init__(self, boot_id: str) -> None:
        self.boot_id = boot_id
        self.seq = 0
        self.pressure = False
        self._entries: list[dict] = []

    @staticmethod
    def new_boot_id() -> str:
        return "".join(secrets.choice(_ALPHABET) for _ in range(12))

    def next_event(self, kind: str, body: dict, ts: datetime) -> dict:
        self.seq += 1
        envelope = {
            "event_id": f"{self.boot_id}-{self.seq}",
            "boot_id": self.boot_id,
            "seq": self.seq,
            "ts": ts,
            "event": {"kind": kind, **body},
        }
        self._entries.append(envelope)
        self._update_pressure()
        return copy.deepcopy(envelope)

    def ack(self, boot_id: str, seq: int) -> None:
        """Cumulative within `boot_id`; other boots' entries are untouched."""
        self._entries = [
            e
            for e in self._entries
            if not (e["boot_id"] == boot_id and e["seq"] <= seq)
        ]
        self._update_pressure()

    def _update_pressure(self) -> None:
        pending = len(self._entries)
        if self.pressure:
            self.pressure = pending > PRESSURE_CLEAR
        else:
            self.pressure = pending >= PRESSURE_SET

    def unacked(self) -> list[dict]:
        return [copy.deepcopy(e) for e in self._entries]

    def reboot(self, new_boot_id: str) -> None:
        self.boot_id = new_boot_id
        self.seq = 0
