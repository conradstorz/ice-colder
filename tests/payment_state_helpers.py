"""Neutral helper: feed an ``Availability`` a ``payment/state`` document.

Replaces the removed legacy ``set_payment_device("coin_acceptor", ...)`` shim in
tests that only need "the coin device is <status>".
"""

from __future__ import annotations

from ice_contracts.board.payment import PaymentState

_ABSENT = {"present": False, "status": "offline"}


def payment_state(coin: str = "ready", bill: str | None = None) -> PaymentState:
    """A ``PaymentState`` with a coin device at *coin* and the bill validator
    at *bill* (absent when ``None``); no cashless reader."""
    return PaymentState.model_validate(
        {
            "boot_id": "boot0001",
            "seq_last": 0,
            "policy_id": "unset",
            "accepting": True,
            "inhibited_reason": None,
            "escrow_cents": 0,
            "bill_in_escrow_cents": None,
            "devices": {
                "coin": {"present": True, "status": coin},
                "bill": _ABSENT if bill is None else {"present": True, "status": bill},
                "cashless": _ABSENT,
            },
            "tubes": [],
            "exact_change": False,
            "stacker": None,
            "session": None,
        }
    )


def set_coin_status(avail, status: str = "ready") -> None:
    avail.set_payment_state(payment_state(coin=status))
