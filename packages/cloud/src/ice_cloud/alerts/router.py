"""Pure alert routing: which person receives which events (spec 3.1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


class _Event(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def gate(self) -> str | None: ...


@dataclass(frozen=True)
class Recipient:
    account_id: str
    email: str
    role: Literal["owner", "tech"]


_TECH_GATES = frozenset({"safety", "fulfillment"})


def _triggers(event: _Event, role: str) -> bool:
    if event.kind in ("offline", "online"):
        return True
    if event.kind == "raised":
        return role == "owner" or event.gate in _TECH_GATES
    return False


def recipients_for[E: _Event](
    events: Sequence[E], people: Sequence[tuple[Recipient, bool]]
) -> dict[str, list[E]]:
    """Map account_id to the triggering events it receives; muted people get none."""
    out: dict[str, list[E]] = {}
    for person, muted in people:
        if muted:
            continue
        mine = [e for e in events if _triggers(e, person.role)]
        if mine:
            out[person.account_id] = mine
    return out


def muted_ids(people: Sequence[tuple[Recipient, bool]]) -> set[str]:
    return {p.account_id for p, muted in people if muted}
