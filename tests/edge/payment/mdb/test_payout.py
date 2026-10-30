import asyncio

import pytest

from ice_contracts.board.common import CommandAck
from ice_contracts.board.payment import PayoutReason
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.hal.command_dispatcher import CommandTimeout, CompletionTimeout
from ice_edge.payment.mdb.payout import PayoutProtocol
from tests.fakes import FakeTaskRunner, settle


def completion(request_id, status="ok", requested=100, paid=None, detail=None):
    if paid is None:
        paid = requested if status == "ok" else 0
    result = {
        "kind": "payout_result",
        "request_id": request_id,
        "requested_cents": requested,
        "paid_cents": paid,
        "status": status,
        "detail": detail,
    }
    return CommandAck(
        request_id=request_id,
        command="payout",
        status="ok",
        detail=status,
        result=result,
        phase="completed",
    )


def plain(request_id, status="ok", detail=None, command="cash_sale"):
    return CommandAck(
        request_id=request_id, command=command, status=status, detail=detail
    )


class FakeFaults:
    def __init__(self):
        self.raised = []

    def raise_fault(self, code, *, sku=None, outcome=None):
        self.raised.append((code, outcome))


class FakeDispatcher:
    """Scripted: each reply is a callable(request_id)->ack, or an Exception."""

    def __init__(self):
        self.sent = []
        self.replies = []

    async def send_and_await_completion(
        self, subsystem, command, params=None, request_id=None
    ):
        self.sent.append((command, params, request_id))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return completion(request_id, requested=params["cents"])
        return reply(request_id)


class Env:
    def __init__(self):
        self.acked = True
        self.alive = True
        self.disp = FakeDispatcher()
        self.runner = FakeTaskRunner()
        self.runner.attach(asyncio.get_running_loop())
        self.faults = FakeFaults()
        self.paid = []
        self.changed = 0
        self.proto = PayoutProtocol(
            dispatcher=self.disp,
            policy_acked=lambda: self.acked,
            board_alive=lambda: self.alive,
            tasks=self.runner,
            faults=self.faults,
            on_paid=lambda item, cents: self.paid.append((item.request_id, cents)),
            on_changed=self._changed,
        )

    def _changed(self):
        self.changed += 1


@pytest.fixture
async def env():
    return Env()


async def test_fifo_payout_then_cash_sale(env):
    env.proto.pay_out(100, PayoutReason.change, "s1")
    env.proto.cash_sale(200, "s1")
    await settle(6)
    assert [c for c, _, _ in env.disp.sent] == ["payout", "cash_sale"]
    assert env.disp.sent[0][1] == {"cents": 100, "reason": "change", "sale_id": "s1"}
    assert env.proto.idle
    assert env.faults.raised == []


async def test_holds_while_policy_not_acked_then_kick_sends_in_order(env):
    env.acked = False
    env.proto.pay_out(100, PayoutReason.cancel, None)
    env.proto.cash_sale(50, None)
    await settle()
    assert env.disp.sent == []
    assert [p.kind for p in env.proto.pending()] == ["payout", "cash_sale"]
    env.acked = True
    env.proto.kick()
    await settle(6)
    assert [c for c, _, _ in env.disp.sent] == ["payout", "cash_sale"]


async def test_holds_while_board_offline(env):
    env.alive = False
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle()
    assert env.disp.sent == []
    env.alive = True
    env.proto.kick()
    await settle(4)
    assert len(env.disp.sent) == 1


async def test_ok_queued_ack_without_result_is_not_settled_as_paid(env):
    # An ok ack with no payout_result must never count as paid.
    env.disp.replies = [lambda rid: plain(rid, "ok", "queued", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid == []
    assert env.faults.raised == [(FaultCode.PAY_103, "unconfirmed")]


async def test_busy_requeues_on_payout_retry_new_id(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "busy", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert len(env.disp.sent) == 1
    assert [c.label for c in env.runner.scheduled] == ["payout_retry"]
    assert not env.proto.idle
    env.runner.fire("payout_retry")
    await settle(4)
    assert len(env.disp.sent) == 2
    assert env.disp.sent[0][2] != env.disp.sent[1][2]  # rejected: fresh id
    assert env.proto.idle
    assert len(env.paid) == 1


async def test_ok_calls_on_paid(env):
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid == [(env.disp.sent[0][2], 100)]
    assert env.faults.raised == []


async def test_partial_pays_and_raises_pay103(env):
    env.disp.replies = [lambda rid: completion(rid, "partial", requested=100, paid=75)]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid[0][1] == 75
    assert env.faults.raised == [(FaultCode.PAY_103, "partial:75/100")]
    assert env.proto.idle


async def test_scaling_partial(env):
    env.disp.replies = [
        lambda rid: completion(
            rid, "partial", requested=105, paid=100, detail="scaling"
        )
    ]
    env.proto.pay_out(105, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid[0][1] == 100
    assert env.faults.raised == [(FaultCode.PAY_103, "partial:100/105")]


async def test_failed_raises_pay103_no_paid(env):
    env.disp.replies = [lambda rid: completion(rid, "failed", detail="tube_empty")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid == []
    assert env.faults.raised == [(FaultCode.PAY_103, "failed:tube_empty")]
    assert env.proto.idle


async def test_completion_timeout_unconfirmed_never_resent(env):
    env.disp.replies = [CompletionTimeout("mdb", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.faults.raised == [(FaultCode.PAY_103, "unconfirmed")]
    assert env.proto.idle
    env.proto.kick()
    await settle()
    assert len(env.disp.sent) == 1


@pytest.mark.parametrize("detail", ["no_policy", "expired"])
async def test_policy_reject_returns_to_head_and_waits_for_kick(env, detail):
    env.disp.replies = [lambda rid: plain(rid, "rejected", detail, "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    env.proto.cash_sale(10, None)
    await settle(4)
    assert len(env.disp.sent) == 1
    assert [p.kind for p in env.proto.pending()] == ["payout", "cash_sale"]
    assert env.runner.scheduled == []
    env.proto.kick()
    await settle(8)
    assert [c for c, _, _ in env.disp.sent] == ["payout", "payout", "cash_sale"]
    assert env.disp.sent[0][2] != env.disp.sent[1][2]  # rejected: fresh id
    assert env.proto.pending() == []
    assert env.proto.idle


async def test_command_timeout_resends_with_same_id(env):
    env.disp.replies = [CommandTimeout("mdb", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.paid == []
    env.runner.fire("payout_retry")
    await settle(4)
    assert env.disp.sent[0][2] == env.disp.sent[1][2]
    assert len(env.paid) == 1


async def test_cash_sale_queued_counts_as_delivered(env):
    env.disp.replies = [lambda rid: plain(rid, "ok", "queued")]
    env.proto.cash_sale(200, "s")
    await settle(4)
    assert env.faults.raised == []
    assert env.proto.idle


async def test_cash_sale_clamped_raises_pay104(env):
    env.disp.replies = [lambda rid: plain(rid, "ok", "clamped")]
    env.proto.cash_sale(200, "s")
    await settle(4)
    assert env.faults.raised == [
        (FaultCode.PAY_104, "escrow_mismatch:cash_sale_clamped")
    ]
    assert env.proto.idle


async def test_on_changed_fires_on_every_queue_change(env):
    env.acked = False
    env.proto.pay_out(100, PayoutReason.cancel, None)
    assert env.changed == 1
    env.proto.cash_sale(5, None)
    assert env.changed == 2
    env.acked = True
    env.proto.kick()
    await settle(8)
    assert env.changed == 4  # one per settled item


async def test_seed_requeues_in_order_and_sends(env):
    from ice_edge.payment.ports import PendingPayout

    items = [
        PendingPayout("a" * 8, "payout", 100, "cancel", None),
        PendingPayout("b" * 8, "cash_sale", 50, None, None),
    ]
    env.proto.seed(items)
    await settle(8)
    assert [r for _, _, r in env.disp.sent] == ["a" * 8, "b" * 8]


async def test_seed_is_not_called_at_boot_with_pay104():
    # Documented contract: Machine must not seed() while PAY-104 is raised; a
    # snapshot's pending payouts are PAY-104 evidence the operator decides on.
    # seed() itself is unconditional, so an unseeded protocol stays empty.
    env = Env()
    assert env.proto.idle
    assert env.proto.pending() == []


async def test_rejected_retry_snapshot_carries_new_request_id(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "busy", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    first = env.proto.pending()[0].request_id
    await settle(4)
    (item,) = env.proto.pending()
    assert item.request_id != first
    assert env.disp.sent[0][2] == first


async def test_accepted_then_completion_timeout_never_resent(env):
    env.disp.replies = [CompletionTimeout("mdb", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    env.proto.kick()
    await settle(4)
    assert len(env.disp.sent) == 1
    assert env.faults.raised == [(FaultCode.PAY_103, "unconfirmed")]
