"""Escrow ledger: the FIFO credit ledger behind ``VMC.credit_escrow_cents``.

Extracted from ``ice_edge.sale.vmc.VMC`` as the second step in breaking up the
VMC god object, following the same pattern as ``ice_edge/faults/fault_registry.py``'s
``FaultRegistry`` (see ``CLAUDE.md``'s "FSM Core" section). ``EscrowLedger``
owns ``total_cents`` (the authoritative escrow amount) and ``credits`` (the FIFO
list of ``Credit``) plus the pure bookkeeping to deposit, consume-FIFO,
restore, and empty them -- including the divergence-guard warning. It knows
nothing about MQTT, the FSM, refund commands, or sale recording; those side
effects and VMC-specific decisions (what to credit when a sale has no
recorded shares, whether a deposit should be refunded during a maintenance
lease) stay on ``VMC`` itself.
"""

from __future__ import annotations

import time

from loguru import logger

from ice_contracts.money import format_cents

from ice_edge.ledger.session_store import Credit


class EscrowLedger:
    """FIFO ledger of credit held in escrow for the customer, in integer cents.

    Invariant, exactly: ``total_cents == sum(c.cents for c in credits)``
    whenever no sale is in flight. Every method here that changes one side
    changes the other, with two exceptions: ``consume_fifo`` deliberately
    changes only ``credits`` and leaves the caller to subtract the price from
    ``total_cents`` (see its docstring), and a caller poking
    ``total_cents``/``credits`` directly -- the divergence guard in
    ``consume_fifo`` exists for exactly that case.
    """

    def __init__(self) -> None:
        self.total_cents: int = 0
        self.credits: list[Credit] = []

    @property
    def has_credit(self) -> bool:
        """Whether there is any credit left in escrow."""
        return self.total_cents > 0

    @property
    def is_empty(self) -> bool:
        """Whether nothing is left in escrow."""
        return self.total_cents <= 0

    def deposit(
        self, method: str, cents: int, ts: float, event_id: str | None = None
    ) -> None:
        """Add a credit: bump the total and append it to the FIFO list."""
        self.total_cents += cents
        self.credits.append(
            Credit(method=method, cents=cents, ts=ts, event_id=event_id)
        )

    def consume_fifo(self, price_cents: int) -> dict[str, int]:
        """Consume credits FIFO for `price_cents`, returning consumed shares.

        Mutates `credits` in place: fully-consumed credits are removed, a
        partially-consumed credit shrinks in place (same method, reduced
        amount), and untouched credits are left exactly as they were. The
        returned dict sums each raw method string to the cents of it that
        was spent -- the method breakdown the caller records for the sale,
        and what a failed vend re-credits via `restore` below, so it must
        never be re-derived from anything but the credits actually consumed
        here.

        This method does NOT subtract the price from `total_cents` -- the
        caller does that itself (see `VMC.process_payment`).

        Divergence guard: `credits` is supposed to sum to `total_cents` at all
        times (every path that changes one changes the other). If it does
        not -- a bug elsewhere, e.g. `total_cents` mutated directly without
        going through `deposit` -- the ledger cannot be trusted to attribute
        this sale correctly, so no credit is touched and the whole price is
        booked to the single method "unknown" instead of silently
        misattributing it to whatever methods happen to be in the (wrong)
        list. This is a bug guard, not an expected path.
        """
        ledger_total = sum(c.cents for c in self.credits)
        if ledger_total != self.total_cents:
            logger.warning(
                f"escrow_credits total ({format_cents(ledger_total)}) diverged from "
                f"credit_escrow ({format_cents(self.total_cents)}); booking "
                f"{format_cents(price_cents)} to 'unknown' rather than misattribute it"
            )
            return {"unknown": price_cents}

        remaining = price_cents
        shares: dict[str, int] = {}
        kept: list[Credit] = []
        for credit in self.credits:
            if remaining <= 0:
                kept.append(credit)
                continue
            take = min(credit.cents, remaining)
            shares[credit.method] = shares.get(credit.method, 0) + take
            remaining -= take
            leftover = credit.cents - take
            if leftover > 0:
                kept.append(
                    Credit(
                        method=credit.method,
                        cents=leftover,
                        ts=credit.ts,
                        event_id=credit.event_id,
                    )
                )
        self.credits = kept
        return shares

    def restore(self, shares: dict[str, int], ts: float, price_cents: int) -> None:
        """Re-credit `price_cents` as separate Credits, one per (method, cents)
        share, in the same order, all stamped with `ts`.

        A zero-cent share is skipped (it contributes nothing to add), but
        `price_cents` is still added to `total_cents` in full -- matching
        `VMC.on_vend_failed`'s pre-extraction behavior exactly.
        """
        self.total_cents += price_cents
        for method, cents in shares.items():
            if cents > 0:
                self.credits.append(Credit(method=method, cents=cents, ts=ts))

    def restore_shares(self, shares: dict[str, int]) -> int:
        """`restore` for a whole sale's shares; returns the cents restored."""
        price = sum(shares.values())
        self.restore(shares, time.time(), price)
        return price

    def take_all(self) -> int:
        """Zero both total and credits, returning the previous total in cents."""
        cents = self.total_cents
        self.total_cents = 0
        self.credits = []
        return cents

    def snapshot_credits(self) -> list[Credit]:
        """A shallow copy of the current credits list, for session snapshots."""
        return list(self.credits)
