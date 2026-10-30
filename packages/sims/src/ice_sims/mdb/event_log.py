"""Board-side replay log for `payment/event` envelopes."""

import secrets
import string
from datetime import datetime

MAX_UNACKED = 256
_ALPHABET = string.ascii_lowercase + string.digits


class EventLog:
    def __init__(self, boot_id: str) -> None:
        self.boot_id = boot_id
        self.seq = 0
        self.overflow_dropped = 0
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
        if len(self._entries) > MAX_UNACKED:
            self._entries.pop(0)
            self.overflow_dropped += 1
        return envelope

    def ack(self, boot_id: str, seq: int) -> None:
        """Cumulative within `boot_id`; other boots' entries are untouched."""
        self._entries = [
            e
            for e in self._entries
            if not (e["boot_id"] == boot_id and e["seq"] <= seq)
        ]

    def unacked(self) -> list[dict]:
        return list(self._entries)

    def reboot(self, new_boot_id: str) -> None:
        self.boot_id = new_boot_id
        self.seq = 0
