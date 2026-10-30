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

    ``request_id`` is minted once at enqueue and never replaced.
    """

    request_id: str
    kind: Literal["payout", "cash_sale"]
    cents: int
    reason: str | None = None
    sale_id: str | None = None


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
