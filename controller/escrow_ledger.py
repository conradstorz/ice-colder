"""Escrow ledger: the FIFO credit ledger behind ``VMC.credit_escrow``.

Extracted from ``controller.vmc.VMC`` as the second step in breaking up the
VMC god object, following the same pattern as ``controller/fault_registry.py``'s
``FaultRegistry`` (see ``CLAUDE.md``'s "FSM Core" section). ``EscrowLedger``
owns ``total`` (the authoritative escrow amount) and ``credits`` (the FIFO
list of ``Credit``) plus the pure bookkeeping to deposit, consume-FIFO,
restore, and empty them -- including the divergence-guard warning. It knows
nothing about MQTT, the FSM, refund commands, or sale recording; those side
effects and VMC-specific decisions (what to credit when a sale has no
recorded shares, whether a deposit should be refunded during a maintenance
lease) stay on ``VMC`` itself.
"""

from __future__ import annotations

from loguru import logger

from services.session_store import Credit


class EscrowLedger:
    """FIFO ledger of credit held in escrow for the customer.

    Invariant, to the cent: ``round(total, 2) == round(sum(c.amount for c in
    credits), 2)`` whenever no sale is in flight. ``total`` is a running
    float sum, so it may carry sub-cent residue (0.1 + 0.2 leaves
    0.30000000000000004); compare it rounded, never with ``==``. Every
    method here that changes one side changes the other, with two
    exceptions: ``consume_fifo`` deliberately changes only ``credits`` and
    leaves the caller to subtract the price from ``total`` (see its
    docstring), and a caller poking ``total``/``credits`` directly -- the
    divergence guard in ``consume_fifo`` exists for exactly that case.
    """

    # Amounts within this many dollars of each other are the same money for
    # ledger purposes -- see VMC.CREDIT_TOLERANCE, which equals this.
    TOLERANCE = 0.005

    def __init__(self) -> None:
        self.total: float = 0.0
        self.credits: list[Credit] = []

    @property
    def has_credit(self) -> bool:
        """Whether there is any credit left in escrow."""
        return self.total > 0

    @property
    def is_empty_within_tolerance(self) -> bool:
        """Whether the remaining total is within tolerance of zero."""
        return self.total <= self.TOLERANCE

    def deposit(self, method: str, amount: float, ts: float) -> None:
        """Add a credit: bump the total and append it to the FIFO list."""
        self.total += amount
        self.credits.append(Credit(method=method, amount=amount, ts=ts))

    def consume_fifo(self, price: float) -> dict[str, float]:
        """Consume credits FIFO for `price`, returning consumed shares.

        Mutates `credits` in place: fully-consumed credits are removed, a
        partially-consumed credit shrinks in place (same method, reduced
        amount), and untouched credits are left exactly as they were. The
        returned dict sums each raw method string to the amount of it that
        was spent -- the method breakdown the caller records for the sale,
        and what a failed vend re-credits via `restore` below, so it must
        never be re-derived from anything but the credits actually consumed
        here.

        This method does NOT subtract `price` from `total` -- the caller
        does that itself (see `VMC.process_payment`), mirroring the split
        that existed before this extraction.

        Divergence guard: `credits` is supposed to sum to `total` at all
        times (every path that changes one changes the other). If it does
        not -- a bug elsewhere, e.g. `total` mutated directly without going
        through `deposit` -- the ledger cannot be trusted to attribute this
        sale correctly, so no credit is touched and the whole price is
        booked to the single method "unknown" instead of silently
        misattributing it to whatever methods happen to be in the (wrong)
        list. This is a bug guard, not an expected path.
        """
        ledger_total = round(sum(c.amount for c in self.credits), 2)
        if abs(ledger_total - round(self.total, 2)) > self.TOLERANCE:
            logger.warning(
                f"escrow_credits total (${ledger_total:.2f}) diverged from "
                f"credit_escrow (${self.total:.2f}); booking "
                f"${price:.2f} to 'unknown' rather than misattribute it"
            )
            return {"unknown": round(price, 2)}

        remaining = round(price, 2)
        shares: dict[str, float] = {}
        kept: list[Credit] = []
        for credit in self.credits:
            if remaining <= self.TOLERANCE:
                kept.append(credit)
                continue
            take = round(min(credit.amount, remaining), 2)
            shares[credit.method] = round(shares.get(credit.method, 0.0) + take, 2)
            remaining = round(remaining - take, 2)
            leftover = round(credit.amount - take, 2)
            if leftover > self.TOLERANCE:
                kept.append(Credit(method=credit.method, amount=leftover, ts=credit.ts))
        self.credits = kept
        return shares

    def restore(self, shares: dict[str, float], ts: float, price: float) -> None:
        """Re-credit `price` as separate Credits, one per (method, amount)
        share, in the same order, all stamped with `ts`.

        A zero-amount share is skipped (it contributes nothing to add), but
        `price` is still added to `total` in full -- matching
        `VMC.on_vend_failed`'s pre-extraction behavior exactly.
        """
        self.total += price
        for method, amount in shares.items():
            if amount > 0:
                self.credits.append(Credit(method=method, amount=amount, ts=ts))

    def take_all(self) -> float:
        """Zero both total and credits, returning the previous rounded total."""
        amount = round(self.total, 2)
        self.total = 0.0
        self.credits = []
        return amount

    def snapshot_credits(self) -> list[Credit]:
        """A shallow copy of the current credits list, for session snapshots."""
        return list(self.credits)
