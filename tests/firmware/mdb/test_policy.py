"""set_policy acks, state echo, and event-log id invariants."""

from ice_contracts.board.payment import PaymentEventEnvelope

from tests.firmware.mdb.helpers import ACK_TIMEOUT, fresh_state, policy_params


async def _apply(board, accept: bool) -> dict:
    params = policy_params(accept=accept)
    since = board.mark()
    rid = await board.send("set_policy", params)
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "ok"
    return await board.wait_for(
        "payment/state",
        lambda p: p["policy_id"] == params["policy_id"],
        ACK_TIMEOUT,
        since=since,
    )


async def test_set_policy_is_acked_and_echoed_in_state(board):
    state = await _apply(board, True)
    assert state["accepting"] is True


async def test_set_policy_accept_false_stops_accepting(board):
    state = await _apply(board, False)
    assert state["accepting"] is False


async def test_malformed_set_policy_is_rejected(board):
    since = board.mark()
    rid = await board.send("set_policy", {"policy_id": "conf-bad"})
    (ack,) = await board.acks(rid, 1, ACK_TIMEOUT, since=since)
    assert ack.status == "rejected"


async def test_boot_id_matches_state_and_every_event(board):
    await _apply(board, False)
    state = await fresh_state(board)
    for raw in board.history("payment/event"):
        env = PaymentEventEnvelope.model_validate(raw)
        assert env.boot_id == state.boot_id
        assert env.event_id == f"{env.boot_id}-{env.seq}"
