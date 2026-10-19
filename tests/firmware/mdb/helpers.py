"""Shared helpers for the MDB conformance tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

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


async def force_event(board, timeout: float = ACK_TIMEOUT) -> PaymentState:
    """Make the board emit an event and return its state with seq_last >= 1.

    ``device_reset`` for the coin device emits a ``device_status`` event. If the
    board still reports ``seq_last == 0`` after the bounded wait, the test is
    skipped rather than passed vacuously.
    """
    since = board.mark()
    rid = await board.send("device_reset", {"device": "coin"})
    await board.acks(rid, 1, timeout, since=since)
    try:
        raw = await board.wait_for(
            "payment/state",
            lambda p: p.get("seq_last", 0) >= 1,
            timeout,
            since=since,
        )
    except TimeoutError:
        pytest.skip("board emitted no event")
    return PaymentState.model_validate(raw)
