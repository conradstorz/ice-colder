import pytest

from ice_contracts.board.vending_machine import FaultCode
from ice_edge.ledger.payment_cursor import PaymentCursorStore
from ice_edge.payment.mdb.intake import EventIntake

BOOT = "boot-0001"


def env(seq, event, boot=BOOT, eid=None):
    return {
        "event_id": eid or f"evt-{boot}-{seq:04d}",
        "boot_id": boot,
        "seq": seq,
        "ts": "2026-10-30T12:00:00Z",
        "event": event,
    }


COIN = {"kind": "coin_accepted", "cents": 25, "routed": "tubes"}
BILL = {"kind": "bill_stacked", "cents": 500}


class FakeFaults:
    def __init__(self):
        self.active = set()
        self.raised = []
        self.cleared = []

    def has(self, code):
        return code in self.active

    def raise_fault(self, code, *, sku=None, outcome=None):
        self.active.add(code)
        self.raised.append((code, outcome))

    def clear_fault(self, key, by="admin"):
        self.cleared.append(key)
        self.active = {c for c in self.active if c.value != key}
        return True


class Rig:
    def __init__(
        self,
        path,
        fail_save=False,
        persist_error=None,
        event_error=None,
        deposit_error=None,
    ):
        self.calls = []
        self.saved = []
        self.deposit_error = deposit_error
        self.faults = FakeFaults()
        self.store = PaymentCursorStore(path)
        orig = self.store.save

        def save(state):
            if fail_save:
                raise OSError("disk")
            self.calls.append("cursor")
            orig(state)

        self.store.save = save

        async def persist():
            self.calls.append("persist")
            if persist_error:
                raise persist_error

        def record(t, v, m):
            if event_error:
                raise event_error
            self.events.append((t, v, m))

        self.lever = 0
        self.payouts = []
        self.statuses = []
        self.journal = []
        self.events = []
        self.deposits = []
        self.acks = []
        self.intake = EventIntake(
            cursor=self.store,
            publish_ack=lambda a: (self.calls.append("ack"), self.acks.append(a)),
            deposit=self._deposit,
            persist_session=persist,
            on_lever=lambda: setattr(self, "lever", self.lever + 1),
            on_payout_result=lambda r, b: self.payouts.append((r, b)),
            on_device_status=self.statuses.append,
            journal=lambda n, d: self.journal.append((n, d)),
            record_event=record,
            faults=self.faults,
            on_cursor_saved=self.saved.append,
        )

    def _deposit(self, c, m, e):
        if self.deposit_error:
            raise self.deposit_error
        self.calls.append("deposit")
        self.deposits.append((c, m, e))

    async def send(self, seq, event, **kw):
        await self.intake.handle("payment/event", env(seq, event, **kw))


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "c.json")


async def test_first_event_credited_and_acked(rig):
    await rig.send(1, COIN)
    assert rig.deposits == [(25, "cash_coin", f"evt-{BOOT}-0001")]
    assert rig.events == [
        ("payment", 25, {"method": "cash_coin", "event_id": f"evt-{BOOT}-0001"})
    ]
    assert [(a.boot_id, a.seq) for a in rig.acks] == [(BOOT, 1)]
    assert rig.intake.caught_up_to(BOOT) == 1
    assert rig.intake.last_processed_ts is not None
    assert not rig.faults.raised


async def test_bill_stacked_is_cash_bill(rig):
    await rig.send(1, BILL)
    assert rig.deposits[0][:2] == (500, "cash_bill")


def test_methods_classify_as_cash():
    from ice_edge.ledger.event_recorder import is_cash

    assert is_cash("cash_coin") and is_cash("cash_bill")


async def test_duplicate_event_id_reacked_not_credited(rig):
    await rig.send(1, COIN)
    await rig.send(2, COIN, eid=f"evt-{BOOT}-0001")
    assert len(rig.deposits) == 1
    assert [a.seq for a in rig.acks] == [1, 2]


async def test_seq_at_or_below_cursor_reacked(rig):
    await rig.send(1, COIN)
    await rig.send(2, COIN)
    await rig.send(1, COIN, eid="evt-other-0001")
    assert len(rig.deposits) == 2
    assert rig.acks[-1].seq == 1


async def test_gap_raises_pay105_and_processes(rig):
    await rig.send(1, COIN)
    await rig.send(3, COIN)
    assert len(rig.deposits) == 2
    assert [c for c, _ in rig.faults.raised] == [FaultCode.PAY_105]
    assert "gaps=1" in rig.faults.raised[0][1]


async def test_pay105_raised_once_while_active(rig):
    await rig.send(1, COIN)
    await rig.send(3, COIN)
    await rig.send(6, COIN)
    assert len(rig.faults.raised) == 1


async def test_unseen_boot_with_seq_gt_1_raises(rig):
    await rig.send(4, COIN)
    assert rig.faults.has(FaultCode.PAY_105)
    assert len(rig.deposits) == 1


async def test_unknown_kind_pay105_and_acked(rig):
    await rig.send(1, {"kind": "from_the_future", "x": 1})
    assert rig.faults.has(FaultCode.PAY_105)
    assert "unknown_kinds=1" in rig.faults.raised[0][1]
    assert rig.acks[-1].seq == 1 and not rig.deposits


async def test_malformed_body_pay105_and_acked(rig):
    await rig.send(1, {"kind": "coin_accepted", "cents": "lots"})
    assert rig.faults.has(FaultCode.PAY_105)
    assert rig.acks[-1].seq == 1 and not rig.deposits


async def test_invalid_header_dropped_no_ack(rig):
    await rig.intake.handle("payment/event", {"event_id": "x"})
    assert not rig.acks and not rig.calls


async def test_ack_after_deposit_persist_and_cursor(rig):
    await rig.send(1, COIN)
    assert rig.calls == ["deposit", "persist", "cursor", "ack"]


async def test_cursor_save_failure_reports_via_callback_and_still_acks(tmp_path):
    rig = Rig(tmp_path / "c.json", fail_save=True)
    await rig.send(1, COIN)
    assert rig.saved == [False]
    assert not rig.faults.raised
    assert len(rig.acks) == 1 and len(rig.deposits) == 1


async def test_cursor_save_success_reports_true(rig):
    await rig.send(1, COIN)
    assert rig.saved == [True]


async def test_persist_session_failure_still_advances_saves_and_acks(tmp_path):
    rig = Rig(tmp_path / "c.json", persist_error=OSError("snap"))
    await rig.send(1, COIN)
    assert len(rig.acks) == 1 and rig.saved == [True]
    await rig.send(2, COIN, eid=f"evt-{BOOT}-0001")  # replay of event 1
    assert len(rig.deposits) == 1


async def test_record_event_failure_still_saves_and_acks_same_call(tmp_path):
    rig = Rig(tmp_path / "c.json", event_error=RuntimeError("queue full"))
    await rig.send(1, COIN)
    assert len(rig.deposits) == 1
    assert rig.saved == [True]
    assert [a.seq for a in rig.acks] == [1]
    await rig.send(1, COIN)  # replay
    assert len(rig.deposits) == 1


async def test_deposit_failure_no_advance_no_ack_then_replay_credits_once(tmp_path):
    rig = Rig(tmp_path / "c.json", deposit_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await rig.send(1, COIN)
    assert not rig.acks and rig.saved == []
    assert rig.intake.caught_up_to(BOOT) == 0
    rig.deposit_error = None
    await rig.send(1, COIN)
    assert len(rig.deposits) == 1
    assert [a.seq for a in rig.acks] == [1]


async def test_replay_after_restart_acks_without_crediting(tmp_path):
    path = tmp_path / "c.json"
    first = Rig(path)
    await first.send(1, COIN)
    second = Rig(path)
    await second.send(1, COIN)
    assert not second.deposits
    assert [a.seq for a in second.acks] == [1]


async def test_seeded_snapshot_ids_prevent_double_credit(tmp_path):
    rig = Rig(tmp_path / "c.json")
    rig.intake.seed(["evt-lost-cursor"])
    await rig.send(1, COIN, eid="evt-lost-cursor")
    assert not rig.deposits
    assert len(rig.acks) == 1


async def test_seeded_duplicate_advances_the_cursor_so_the_next_event_is_no_gap(
    tmp_path,
):
    rig = Rig(tmp_path / "c.json")
    rig.intake.seed([f"evt-{BOOT}-0001"])
    await rig.send(1, COIN)  # the replayed, already-credited event
    await rig.send(2, COIN)
    assert not rig.faults.has(FaultCode.PAY_105)
    assert len(rig.deposits) == 1  # only seq 2 is new
    assert rig.intake.caught_up_to(BOOT) == 2


async def test_corrupt_cursor_at_boot_raises_pay105(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("garbage", encoding="utf-8")
    rig = Rig(p)
    assert rig.faults.has(FaultCode.PAY_105)


async def test_bill_escrowed_is_acked_without_deposit(rig):
    await rig.send(1, {"kind": "bill_escrowed", "cents": 500})
    assert not rig.deposits and rig.acks


async def test_bill_returned_journal(rig):
    await rig.send(1, {"kind": "bill_returned", "cents": 500, "reason": "policy"})
    assert rig.journal == [("bill_returned", {"cents": 500, "reason": "policy"})]


async def test_lever(rig):
    await rig.send(1, {"kind": "escrow_return_pressed"})
    assert rig.lever == 1


async def test_payout_result(rig):
    body = {
        "kind": "payout_result",
        "request_id": "req-00001",
        "requested_cents": 75,
        "paid_cents": 75,
        "status": "ok",
    }
    await rig.send(1, body)
    assert rig.payouts[0][0] == "req-00001"
    assert rig.payouts[0][1].paid_cents == 75


async def test_device_status(rig):
    await rig.send(1, {"kind": "device_status", "device": "coin", "status": "jammed"})
    assert rig.statuses[0].status.value == "jammed"


async def test_cashless_ignored_and_acked(rig):
    await rig.send(
        1, {"kind": "session_begun", "session_id": "s1", "funds_cents": None}
    )
    assert not rig.deposits and not rig.faults.raised and len(rig.acks) == 1


async def test_cash_collected_event_row_only(rig):
    await rig.send(1, {"kind": "cash_collected", "device": "bill"})
    assert rig.events == [("cash_collected", 0, {"device": "bill"})]
    assert not rig.deposits
