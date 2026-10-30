from datetime import datetime, timezone

from ice_contracts.board.payment import PaymentEventEnvelope
from ice_sims.mdb.event_log import MAX_UNACKED, EventLog

TS = datetime(2026, 10, 30, tzinfo=timezone.utc)
BOOT = "bootaaaa0001"


def emit(log: EventLog, n: int = 1):
    return [
        log.next_event("coin_accepted", {"cents": 25, "routed": "tubes"}, TS)
        for _ in range(n)
    ]


def test_ids_and_seq_validate():
    log = EventLog(BOOT)
    a, b = emit(log, 2)
    assert (a["seq"], b["seq"]) == (1, 2)
    assert a["event_id"] == f"{BOOT}-1"
    for env in (a, b):
        PaymentEventEnvelope.model_validate(env)


def test_new_boot_id():
    ids = {EventLog.new_boot_id() for _ in range(20)}
    assert all(len(i) == 12 for i in ids)
    assert len(ids) > 1


def test_cumulative_ack():
    log = EventLog(BOOT)
    emit(log, 4)
    log.ack(BOOT, 2)
    assert [e["seq"] for e in log.unacked()] == [3, 4]
    log.ack("otherboot001", 99)
    assert len(log.unacked()) == 2


def test_bounded_log_drops_oldest_and_records():
    log = EventLog(BOOT)
    emit(log, MAX_UNACKED + 3)
    un = log.unacked()
    assert len(un) == MAX_UNACKED
    assert un[0]["seq"] == 4
    assert log.overflow_dropped == 3


def test_ordered_replay_across_reboot():
    log = EventLog(BOOT)
    emit(log, 2)
    log.reboot("bootbbbb0002")
    emit(log, 2)
    got = [(e["boot_id"], e["seq"]) for e in log.unacked()]
    assert got == [(BOOT, 1), (BOOT, 2), ("bootbbbb0002", 1), ("bootbbbb0002", 2)]
    log.ack(BOOT, 2)
    assert [e["boot_id"] for e in log.unacked()] == ["bootbbbb0002"] * 2
    for e in log.unacked():
        PaymentEventEnvelope.model_validate(e)
