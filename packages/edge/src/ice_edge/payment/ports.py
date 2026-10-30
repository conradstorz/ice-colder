"""Ports the sale layer types against, so the VMC never imports the dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ice_contracts.board.payment import PayoutReason


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
