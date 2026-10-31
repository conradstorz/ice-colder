"""Board escrow vs. edge cash escrow (cutover spec 5.5, contract flows 7 and 8).

The board's escrow is RAM only and restarts at 0 on every boot (firmware spec),
so only edge cash credited on the board's *current* boot is comparable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from ice_contracts.board.payment import PaymentState, PayoutReason
from ice_contracts.board.vending_machine import FaultCode


class Reconciler:
    def __init__(
        self,
        *,
        state: Callable[[], PaymentState | None],
        state_ts: Callable[[], float],
        intake: Any,
        payouts: Any,
        fsm_idle: Callable[[], bool],
        pay104_active: Callable[[], bool],
        edge_cash_escrow: Callable[[str], int],
        faults: Any,
    ) -> None:
        self._state = state
        self._state_ts = state_ts
        self._intake = intake
        self._payouts = payouts
        self._fsm_idle = fsm_idle
        self._pay104_active = pay104_active
        self._edge_cash = edge_cash_escrow
        self._faults = faults
        self._armed = False
        self._boot: str | None = None

    def arm(self, trigger: str) -> None:
        logger.info(f"MDB reconciliation armed: {trigger}")
        self._armed = True

    def on_state(self, state: PaymentState) -> None:
        if self._boot is not None and state.boot_id != self._boot:
            self.arm("new_boot")
        self._boot = state.boot_id
        self.maybe_run()

    def maybe_run(self) -> None:
        if not self._armed:
            return
        state = self._state()
        if state is None or not self._fsm_idle() or self._pay104_active():
            return
        if self._intake.caught_up_to(state.boot_id) < state.seq_last:
            return
        if not self._payouts.idle:
            return
        last = self._intake.last_processed_ts
        if last is not None and self._state_ts() <= last:
            return
        done = getattr(self._payouts, "last_done_ts", None)
        if done is not None and self._state_ts() <= done:
            return  # this state predates the last payout / cash_sale
        self._armed = False
        edge, board = self._edge_cash(state.boot_id), state.escrow_cents
        if edge == board:
            return
        if board > edge:
            logger.warning(f"MDB board holds {board - edge} cents more than edge")
            self._payouts.pay_out(board - edge, PayoutReason.error, None)
        else:
            self._faults.raise_fault(
                FaultCode.PAY_104, outcome=f"escrow_mismatch:{edge}/{board}"
            )
