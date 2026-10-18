"""Malformed and unknown commands."""


async def test_unknown_command_is_unsupported(board):
    rid = await board.send("no_such_command")
    (ack,) = await board.acks(rid, 1, 5)
    assert ack.status == "unsupported"


async def test_command_without_request_id_gets_no_ack_and_board_stays_up(board):
    await board.publish_raw("cmd/vending", {"command": "ping", "params": {}})
    assert await board.collect("cmd/vending/ack", 3) == []
    rid = await board.send("ping")
    (ack,) = await board.acks(rid, 1, 5)
    assert ack.status == "ok"


async def test_dispense_without_profile_is_rejected(board):
    rid = await board.send("dispense", {"slot": 0, "mechanism": "bagged_ice"})
    (ack,) = await board.acks(rid, 1, 5)
    assert ack.status == "rejected"
