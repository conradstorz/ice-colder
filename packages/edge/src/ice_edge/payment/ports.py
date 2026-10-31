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

    def board_boot_id(self) -> str | None:
        """The board's current boot id (its escrow is RAM only, per boot), or
        None while unknown."""
        ...


def cash_cents(credits: Iterable[Credit], boot_id: str | None = None) -> int:
    """The part of ``credits`` that arrived as cash, i.e. the only money the
    MDB board can pay back out. With ``boot_id`` only cash whose board event id
    belongs to that boot counts (the board's escrow is per boot)."""
    return current_boot_cash(credits, boot_id)


def current_boot_cash(pieces: Iterable[Credit], boot_id: str | None) -> int:
    """Cash among ``pieces`` that arrived on the board's current boot.

    The board's escrow restarts at 0 on every boot, so only such cash can be
    reported to it with ``cash_sale``; earlier-boot cash is paid back as change
    from the tubes. With ``boot_id`` unknown every cash piece counts; a credit
    without an event id never belongs to a known boot.
    """
    return sum(
        c.cents
        for c in pieces
        if is_cash(c.method)
        and (boot_id is None or (c.event_id or "").startswith(f"{boot_id}-"))
    )


def cash_share(shares: dict[str, int] | None) -> int:
    """The cash part of a sale's per-method ``shares`` (method -> cents)."""
    return sum(c for m, c in (shares or {}).items() if is_cash(m))
