"""ping, self_test and force_report."""


async def test_ping_acks_ok_completed(board):
    rid = await board.send("ping")
    (ack,) = await board.acks(rid, 1, 5)
    assert (ack.command, ack.status, ack.phase) == ("ping", "ok", "completed")


async def test_self_test_returns_named_checks(board):
    rid = await board.send("self_test")
    (ack,) = await board.acks(rid, 1, 10)
    assert ack.status == "ok"
    checks = ack.result["checks"]
    assert checks
    for check in checks:
        assert check["name"]
        assert isinstance(check["pass"], bool)
        assert isinstance(check["detail"], str)


async def test_force_report_republishes_capabilities_and_io(board):
    since = board.mark()
    rid = await board.send("force_report")
    (ack,) = await board.acks(rid, 1, 5)
    assert ack.status == "ok"
    await board.wait_for("capabilities/vending", lambda _p: True, 5, since=since)
    await board.wait_for("hardware/io/+", lambda _p: True, 5, since=since)
