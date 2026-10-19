"""Money-moving cash commands: expiry, no_bill, clamping."""

import pytest

from tests.firmware.mdb.helpers import ACK_TIMEOUT, fresh_state, old_timestamp


async def test_expired_payout_is_rejected(board):
    since = board.mark()
    rid = await board.send(
        "payout",
        {"cents": 5, "reason": "cancel", "sale_id": None},
        timestamp=old_timestamp(200),
    )
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "rejected"
    assert ack.detail == "expired"


async def test_return_escrow_without_a_bill_acks_no_bill(board):
    state = await fresh_state(board)
    if state.bill_in_escrow_cents is not None:
        pytest.skip("a bill is in escrow on this board")
    since = board.mark()
    rid = await board.send("return_escrow")
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "ok"
    assert ack.detail == "no_bill"


async def test_cash_sale_larger_than_escrow_is_clamped(board):
    state = await fresh_state(board)
    since = board.mark()
    rid = await board.send(
        "cash_sale", {"cents": state.escrow_cents + 1, "sale_id": None}
    )
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "ok"
    assert ack.detail == "clamped"
