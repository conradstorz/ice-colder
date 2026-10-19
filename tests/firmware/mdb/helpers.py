"""Shared helpers for the MDB conformance tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from ice_contracts.board.payment import PaymentState

ACK_TIMEOUT = 10


def policy_params(accept: bool = True, **over) -> dict:
    params = {
        "policy_id": f"conf-{uuid.uuid4().hex[:8]}",
        "accept": accept,
        "max_escrow_cents": 2000,
        "coin_enabled": [5, 10, 25, 100],
        "bill_enabled": [100, 500, 1000, 2000],
        "cashless_enabled": True,
        "change_threshold_cents": 500,
        "edge_timeout_s": 30,
        "return_bill_on_lever": True,
    }
    params.update(over)
    return params


def old_timestamp(seconds: int = 200) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


async def fresh_state(board, timeout: float = 10) -> PaymentState:
    """Force a republish and return the state that follows it."""
    since = board.mark()
    rid = await board.send("force_report")
    await board.acks(rid, 1, timeout, since=since)
    return PaymentState.model_validate(
        await board.wait_for("payment/state", lambda _p: True, timeout, since=since)
    )
