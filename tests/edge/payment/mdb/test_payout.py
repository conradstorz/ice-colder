import asyncio

import pytest

from ice_contracts.board.common import CommandAck
from ice_contracts.board.payment import PayoutReason
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.hal.command_dispatcher import (
    CommandNotSent,
    CommandTimeout,
    CompletionTimeout,
)
from ice_edge.payment.mdb.payout import PayoutProtocol
from tests.fakes import FakeTaskRunner, settle
from tests.fakes import FakeFaults


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
        self.boot = None
        self.proto = PayoutProtocol(
            dispatcher=self.disp,
            policy_acked=lambda: self.acked,
            board_alive=lambda: self.alive,
            tasks=self.runner,
            faults=self.faults,
            on_paid=lambda item, cents: self.paid.append((item.request_id, cents)),
            on_changed=self._changed,
            boot_id=lambda: self.boot,
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


async def test_policy_reject_returns_to_head_and_waits_for_kick(env):
    detail = "no_policy"
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


async def test_unexpected_send_error_requeues_same_id_and_queue_not_wedged(env):
    env.disp.replies = [RuntimeError("publish failed")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    env.proto.cash_sale(10, None)
    await settle(4)
    assert env.paid == [] and not env.proto.idle
    assert [p.kind for p in env.proto.pending()] == ["payout", "cash_sale"]
    assert [c.label for c in env.runner.scheduled] == ["payout_retry"]
    env.runner.fire("payout_retry")
    await settle(8)
    assert env.disp.sent[0][2] == env.disp.sent[1][2]
    assert len(env.paid) == 1
    assert env.proto.idle and env.faults.raised == []


@pytest.mark.parametrize("kind", ["payout", "cash_sale"])
async def test_non_positive_cents_rejected_at_enqueue(env, kind):
    with pytest.raises(ValueError):
        if kind == "payout":
            env.proto.pay_out(0, PayoutReason.cancel, None)
        else:
            env.proto.cash_sale(-5, None)
    assert env.proto.idle and env.changed == 0


async def test_on_paid_exception_still_finishes_no_resend(env):
    def boom(item, cents):
        raise RuntimeError("journal down")

    env.proto = PayoutProtocol(
        dispatcher=env.disp,
        policy_acked=lambda: True,
        board_alive=lambda: True,
        tasks=env.runner,
        faults=env.faults,
        on_paid=boom,
        on_changed=lambda: None,
    )
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.proto.idle
    assert len(env.disp.sent) == 1


async def test_fault_raise_exception_after_failed_result_still_finishes(env):
    def boom(code, *, sku=None, outcome=None):
        raise RuntimeError("faults down")

    env.faults.raise_fault = boom
    env.disp.replies = [lambda rid: completion(rid, "failed", detail="x")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    env.proto.cash_sale(5, None)
    await settle(8)
    assert env.proto.idle
    assert len(env.disp.sent) == 2


async def test_cash_sale_no_policy_requeues_fresh_id_and_holds(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "no_policy")]
    env.proto.cash_sale(50, None)
    await settle(4)
    (item,) = env.proto.pending()
    assert item.request_id != env.disp.sent[0][2]
    assert env.faults.raised == []
    env.proto.kick()
    await settle(4)
    assert len(env.disp.sent) == 2 and env.proto.idle


async def test_cash_sale_other_rejection_is_pay104(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "weird")]
    env.proto.cash_sale(50, None)
    await settle(4)
    assert env.faults.raised[0][0] == FaultCode.PAY_104
    assert env.proto.idle


async def test_expired_retries_with_fresh_id_after_payout_retry(env):
    # "expired" means the command's timestamp was stale, not that the policy is
    # missing: no policy toggle will ever come, so it must retry on its own.
    env.disp.replies = [lambda rid: plain(rid, "rejected", "expired", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert len(env.disp.sent) == 1
    assert any(c.label == "payout_retry" for c in env.runner.scheduled)
    env.runner.fire("payout_retry")
    await settle(6)
    assert len(env.disp.sent) == 2
    assert env.disp.sent[0][2] != env.disp.sent[1][2]
    assert len(env.paid) == 1
    assert env.proto.idle


async def test_cash_sale_expired_retries_with_fresh_id(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "expired")]
    env.proto.cash_sale(50, None)
    await settle(4)
    env.runner.fire("payout_retry")
    await settle(6)
    assert len(env.disp.sent) == 2
    assert env.disp.sent[0][2] != env.disp.sent[1][2]
    assert env.faults.raised == []


async def test_seed_drops_poison_item_raises_pay103_and_does_not_wedge(env):
    from ice_edge.payment.ports import PendingPayout

    items = [
        PendingPayout("p" * 8, "payout", 0, "cancel", None),  # zero cents
        PendingPayout("q" * 8, "payout", 100, "not_a_reason", None),
        PendingPayout("g" * 8, "payout", 100, "cancel", None),
    ]
    env.proto.seed(items)
    await settle(8)
    assert [r for _, _, r in env.disp.sent] == ["g" * 8]
    assert (FaultCode.PAY_103, "invalid_pending:" + "p" * 8) in env.faults.raised
    assert (FaultCode.PAY_103, "invalid_pending:" + "q" * 8) in env.faults.raised
    assert env.proto.idle


# --- C1: never re-mint after a possibly delivered send ---------------------------


async def test_timeout_then_no_policy_rejection_never_reminted_payout(env):
    env.disp.replies = [
        CommandTimeout("mdb", "payout"),
        lambda rid: plain(rid, "rejected", "no_policy", "payout"),
    ]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    first = env.disp.sent[0][2]
    assert env.proto.pending()[0].maybe_delivered is True
    env.runner.fire("payout_retry")
    await settle(6)
    assert [r for _, _, r in env.disp.sent] == [first, first]  # same id, no re-mint
    assert env.faults.raised == [(FaultCode.PAY_103, f"unconfirmed:100:{first}")]
    assert env.paid == []
    assert env.proto.idle
    env.proto.kick()
    await settle()
    assert len(env.disp.sent) == 2


async def test_timeout_then_rejection_never_reminted_cash_sale(env):
    env.disp.replies = [
        CommandTimeout("mdb", "cash_sale"),
        lambda rid: plain(rid, "rejected", "no_policy"),
    ]
    env.proto.cash_sale(50, None)
    await settle(4)
    env.runner.fire("payout_retry")
    await settle(6)
    assert len(env.disp.sent) == 2
    assert env.faults.raised == [
        (FaultCode.PAY_104, "escrow_mismatch:cash_sale_unconfirmed")
    ]
    assert env.proto.idle


@pytest.mark.parametrize("detail", ["busy", "expired", "no_policy"])
async def test_seeded_item_is_maybe_delivered_and_never_reminted(env, detail):
    from ice_edge.payment.ports import PendingPayout

    env.disp.replies = [lambda rid: plain(rid, "rejected", detail, "payout")]
    env.proto.seed([PendingPayout("s" * 8, "payout", 100, "cancel", None)])
    await settle(6)
    assert [r for _, _, r in env.disp.sent] == ["s" * 8]
    assert env.faults.raised == [(FaultCode.PAY_103, "unconfirmed:100:" + "s" * 8)]
    assert env.proto.idle


async def test_generic_send_failure_marks_maybe_delivered(env):
    env.disp.replies = [RuntimeError("boom")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.proto.pending()[0].maybe_delivered is True


async def test_fresh_item_rejection_is_still_reminted(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "busy", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.proto.pending()[0].maybe_delivered is False


async def test_expired_retries_are_capped_at_five_payout(env):
    env.disp.replies = [
        lambda rid: plain(rid, "rejected", "expired", "payout") for _ in range(5)
    ]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    for _ in range(4):
        await settle(4)
        env.runner.fire("payout_retry")
    await settle(6)
    assert len(env.disp.sent) == 5
    last = env.disp.sent[-1][2]
    assert env.faults.raised == [(FaultCode.PAY_103, f"expired:100:{last}")]
    assert env.proto.idle


async def test_expired_retries_are_capped_at_five_cash_sale(env):
    env.disp.replies = [lambda rid: plain(rid, "rejected", "expired") for _ in range(5)]
    env.proto.cash_sale(50, None)
    for _ in range(4):
        await settle(4)
        env.runner.fire("payout_retry")
    await settle(6)
    assert len(env.disp.sent) == 5
    assert env.faults.raised == [
        (FaultCode.PAY_104, "escrow_mismatch:cash_sale_expired")
    ]
    assert env.proto.idle


def test_old_snapshot_dict_loads_with_maybe_delivered_false():
    from ice_edge.payment.ports import PendingPayout

    item = PendingPayout(
        **{"request_id": "r" * 8, "kind": "payout", "cents": 5, "reason": "cancel"}
    )
    assert item.maybe_delivered is False


# --- final round C: not-published sends, stale boot ------------------------------------


async def test_not_sent_command_is_not_maybe_delivered_and_keeps_its_id(env):
    env.disp.replies = [CommandNotSent("mdb", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    head = env.proto.pending()[0]
    assert head.maybe_delivered is False
    env.runner.fire("payout_retry")
    await settle(6)
    assert [r for _, _, r in env.disp.sent] == [head.request_id, head.request_id]
    assert env.faults.raised == []
    assert env.proto.idle


async def test_not_sent_then_rejection_is_still_reminted(env):
    env.disp.replies = [
        CommandNotSent("mdb", "payout"),
        lambda rid: plain(rid, "rejected", "busy", "payout"),
    ]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    env.runner.fire("payout_retry")
    await settle(4)
    assert env.proto.pending()[0].maybe_delivered is False
    assert env.faults.raised == []


async def test_plain_timeout_is_still_maybe_delivered(env):
    env.disp.replies = [CommandTimeout("mdb", "payout")]
    env.proto.pay_out(100, PayoutReason.cancel, None)
    await settle(4)
    assert env.proto.pending()[0].maybe_delivered is True


async def test_cash_sale_is_dropped_when_the_board_rebooted_since_enqueue(env):
    env.alive = False
    env.boot = "boot-a"
    env.proto.cash_sale(150, "s1")
    await settle(4)
    assert env.proto.pending()[0].boot_id == "boot-a"
    env.boot = "boot-b"  # board rebooted and is back
    env.alive = True
    env.proto.kick()
    await settle(6)
    assert env.disp.sent == []
    assert env.faults.raised == []
    assert env.proto.idle


async def test_cash_sale_is_sent_when_the_boot_is_unchanged_or_unknown(env):
    env.boot = "boot-a"
    env.proto.cash_sale(150, "s1")
    await settle(6)
    env.boot = None
    env.proto.cash_sale(50, "s2")
    await settle(6)
    assert [p["cents"] for _, p, _ in env.disp.sent] == [150, 50]


async def test_payout_is_never_dropped_for_a_boot_change(env):
    env.alive = False
    env.boot = "boot-a"
    env.proto.pay_out(100, PayoutReason.cancel, None)
    env.boot = "boot-b"
    env.alive = True
    env.proto.kick()
    await settle(6)
    assert len(env.disp.sent) == 1


def test_old_snapshot_dict_loads_with_no_boot_id():
    from ice_edge.payment.ports import PendingPayout

    item = PendingPayout(**{"request_id": "r" * 8, "kind": "cash_sale", "cents": 5})
    assert item.boot_id is None
