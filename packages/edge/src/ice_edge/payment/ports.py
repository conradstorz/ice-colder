"""Ports the sale layer types against, so the VMC never imports the dispatcher."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from ice_contracts.board.payment import PayoutReason
from ice_edge.ledger.event_recorder import is_cash
from ice_edge.ledger.session_store import Credit


@dataclass(frozen=True)
class PendingPayout:
    """One queued or in-flight ``payout`` / ``cash_sale`` command.

    ``request_id`` is minted at enqueue and replaced only while the board
    provably did nothing with it. ``maybe_delivered`` is set once a send may
    have reached the board (ack lost, error, or a restart reloaded it): such
    an item is never re-minted. ``expired_attempts`` counts ``expired``
    rejections (capped by the protocol).
    """

    request_id: str
    kind: Literal["payout", "cash_sale"]
    cents: int
    reason: str | None = None
    sale_id: str | None = None
    maybe_delivered: bool = False
    expired_attempts: int = 0


class Payments(Protocol):
    def pay_out(
        self, cents: int, reason: PayoutReason, sale_id: str | None
    ) -> None: ...

    def cash_sale(self, cents: int, sale_id: str | None) -> None: ...

    def pending_payouts(self) -> list[PendingPayout]: ...


def cash_cents(credits: Iterable[Credit]) -> int:
    """The part of ``credits`` that arrived as cash, i.e. the only money the
    MDB board can pay back out."""
    return sum(c.cents for c in credits if is_cash(c.method))


def cash_share(shares: dict[str, int] | None) -> int:
    """The cash part of a sale's per-method ``shares`` (method -> cents)."""
    return sum(c for m, c in (shares or {}).items() if is_cash(m))
