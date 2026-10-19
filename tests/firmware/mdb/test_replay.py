"""Event-log replay: acks are cumulative within a boot and never move seq_last."""

from ice_contracts.board.payment import PaymentEventAck, PaymentEventEnvelope

from tests.firmware.mdb.helpers import fresh_state


async def test_current_boot_events_never_exceed_seq_last(board):
    state = await fresh_state(board)
    envelopes = [
        PaymentEventEnvelope.model_validate(p) for p in board.history("payment/event")
    ]
    assert all(e.seq <= state.seq_last for e in envelopes if e.boot_id == state.boot_id)


async def test_acking_seq_last_leaves_state_unchanged(board):
    before = await fresh_state(board)
    if before.seq_last < 1:
        return
    ack = PaymentEventAck(boot_id=before.boot_id, seq=before.seq_last)
    await board.publish_raw("payment/event/ack", ack.model_dump(mode="json"))
    after = await fresh_state(board)
    assert after.boot_id == before.boot_id
    assert after.seq_last == before.seq_last
