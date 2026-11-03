"""Sends money out through the `Payments` port: refunds/change and `cash_sale`.

Split from `VMC` (size ratchet). Holds no FSM state: the VMC passes in what
it knows. Failures never leave money in limbo: a payout that cannot be queued
puts the escrow back and raises PAY-103.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from ice_contracts.board.payment import PayoutReason
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.ledger.escrow_ledger import EscrowLedger
from ice_edge.payment.ports import (
    Payments,
    cash_cents,
    cash_share,
    current_boot_cash,
)
from ice_edge.sale.sale_context import SaleContext
from ice_edge.sale.sale_journal import SaleJournal


class PayoutDesk:
    def __init__(
        self,
        escrow: EscrowLedger,
        payments: Payments,
        journal: SaleJournal,
        raise_fault: Callable[..., object],
    ) -> None:
        self._escrow = escrow
        self._payments = payments
        self._journal = journal
        self._raise = raise_fault

    def refund(
        self, reason: str, detail: str | None, sale_id: str | None, method: str
    ) -> None:
        """Take all of escrow and queue its cash part as a payout.

        The board pays cash only, so a non-cash part is logged at error and
        raised as PAY-103 `non_cash_refund`. If the payout cannot be queued
        the credits go back into escrow untouched and PAY-103
        `payout_not_queued` is raised.
        """
        taken = self._escrow.snapshot_credits()
        cash = cash_cents(taken)
        other = self._escrow.total_cents - cash
        self._escrow.take_all()
        if cash > 0:
            try:
                payout_reason = PayoutReason(reason)
            except ValueError:
                logger.warning(f"Unknown refund reason {reason!r}; paying out as error")
                payout_reason = PayoutReason.error
            try:
                self._payments.pay_out(cash, payout_reason, sale_id)
            except Exception:
                logger.exception("payout could not be queued; escrow restored")
                for c in taken:
                    self._escrow.deposit(c.method, c.cents, c.ts, c.event_id)
                self._raise(FaultCode.PAY_103, outcome="payout_not_queued")
                return
            pending = self._payments.pending_payouts()
            self._journal.refund_requested(
                cash,
                method,
                f"{reason} ({detail})" if detail else reason,
                pending[-1].request_id if pending else "n/a",
            )
        if other > 0:
            self._journal.non_cash_refund(other, reason)
            self._raise(FaultCode.PAY_103, outcome="non_cash_refund")

    def cash_sale(self, sale: SaleContext) -> None:
        """Tell the board the sale's cash left its escrow (cash share only).

        The board's escrow restarts at 0 on every boot, so only cash that
        arrived on its current boot is reported; earlier-boot cash is paid
        back as change from the tubes (the board floors escrow at 0).
        """
        if sale.pieces is None:
            cents = cash_share(sale.shares)
        else:
            cents = current_boot_cash(sale.pieces, self._payments.board_boot_id())
        if cents <= 0:
            return
        try:
            self._payments.cash_sale(cents, sale.sale_id)
        except Exception:
            logger.exception(f"cash_sale could not be queued (sale_id={sale.sale_id})")
            self._raise(FaultCode.PAY_103, outcome="cash_sale_not_queued")
