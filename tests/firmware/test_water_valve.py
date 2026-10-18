"""water_valve: accepted, then completed; out-of-range seconds rejected."""

import pytest


async def test_valve_run_is_accepted_then_completed(board):
    rid = await board.send("water_valve", {"seconds": 2})
    accepted, completed = await board.acks(rid, 2, 7)
    assert (accepted.status, accepted.phase) == ("ok", "accepted")
    assert (completed.status, completed.phase) == ("ok", "completed")


@pytest.mark.parametrize("seconds", [0, 11])
async def test_out_of_range_seconds_is_rejected(board, seconds):
    rid = await board.send("water_valve", {"seconds": seconds})
    (ack,) = await board.acks(rid, 1, 5)
    assert ack.status == "rejected"
    assert "seconds" in (ack.detail or "")
