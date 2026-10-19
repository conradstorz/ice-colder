"""Standard commands and request_id idempotency on cmd/mdb."""

import pytest

from tests.firmware.mdb.helpers import ACK_TIMEOUT


@pytest.mark.parametrize("command", ["ping", "self_test", "force_report"])
async def test_standard_command_acks_completed(board, command):
    since = board.mark()
    rid = await board.send(command)
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.command == command
    assert ack.status == "ok"
    assert ack.phase == "completed"


async def test_force_report_republishes_state(board):
    since = board.mark()
    rid = await board.send("force_report")
    await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    state = await board.wait_for(
        "payment/state", lambda _p: True, ACK_TIMEOUT, since=since
    )
    assert state["boot_id"]


async def test_duplicate_request_id_replays_the_cached_ack(board):
    since = board.mark()
    rid = await board.send("ping")
    (first,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    await board.send("ping", request_id=rid)
    _, replay = await board.acks(rid, 2, ACK_TIMEOUT, since=since)
    assert replay.status == first.status
    assert replay.phase == first.phase
    assert replay.detail == first.detail
