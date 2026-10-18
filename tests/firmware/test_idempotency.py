"""A repeated request_id replays the cached ack and does not repeat the side effect."""

import pytest

VALVE = "hardware/io/water_valve_solenoid"


def _wire(ack) -> dict:
    return ack.model_dump(exclude={"timestamp"})


async def test_duplicate_ping_replays_an_identical_ack(board):
    rid = await board.send("ping")
    await board.send("ping", request_id=rid)
    first, second = await board.acks(rid, 2, 5)
    assert _wire(first) == _wire(second)


@pytest.mark.requires_sim
async def test_duplicate_valve_command_does_not_open_the_valve_twice(board):
    since = board.mark()
    rid = await board.send("water_valve", {"seconds": 1})
    await board.send("water_valve", {"seconds": 1}, request_id=rid)
    await board.acks(rid, 3, 5)  # accepted, completed, and the replayed ack
    await board.collect(VALVE, 1.5)  # a repeated handler would open it again here
    opened = [p for p in board.history(VALVE, since) if p["state"] is True]
    assert len(opened) == 1


async def test_duplicate_after_completion_replays_completed_not_accepted(board):
    rid = await board.send("water_valve", {"seconds": 1})
    await board.acks(rid, 2, 5)
    since = board.mark()
    await board.send("water_valve", {"seconds": 1}, request_id=rid)
    (replayed,) = await board.acks(rid, 1, 5, since=since)
    assert (replayed.status, replayed.phase) == ("ok", "completed")
