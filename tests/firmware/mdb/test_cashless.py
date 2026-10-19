"""Cashless vend flow guards."""

import pytest

from tests.firmware.mdb.helpers import ACK_TIMEOUT, fresh_state


async def test_vend_request_without_a_session_is_rejected(board):
    state = await fresh_state(board)
    if state.session is not None:
        pytest.skip("a cashless session is open on this board")
    since = board.mark()
    rid = await board.send(
        "vend_request", {"session_id": None, "cents": 100, "item": "conf"}
    )
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "rejected"
    assert ack.detail == "no_session"
