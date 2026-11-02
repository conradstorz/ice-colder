import asyncio

import pytest

from ice_common.config_model import MdbPolicyConfig
from ice_contracts.board.common import CommandAck
from ice_contracts.board.payment import EDGE_IDLE_PING_SECONDS, PayoutReason
from ice_edge.ledger.payment_cursor import PaymentCursorStore
from ice_edge.payment.mdb import MdbGateway
from ice_edge.payment.mdb.policy import build_policy
from tests.fakes import FakeTaskRunner, settle

BOOT = "boot-0001"


def doc(policy_id, escrow=0, seq_last=0, boot=BOOT):
    return {
        "boot_id": boot,
        "seq_last": seq_last,
        "policy_id": policy_id,
        "accepting": True,
        "inhibited_reason": None,
        "escrow_cents": escrow,
        "bill_in_escrow_cents": None,
        "devices": {
            "coin": {"present": True, "status": "ready"},
            "bill": {"present": True, "status": "ready"},
            "cashless": {"present": False, "status": "offline"},
        },
        "tubes": [],
        "exact_change": False,
        "stacker": None,
        "session": None,
    }


class FakeFaults:
    def __init__(self):
        self.raised = []

    def has(self, code):
        return False

    def raise_fault(self, code, *, sku=None, outcome=None):
        self.raised.append((code, outcome))

    def clear_fault(self, key, by="admin"):
        return True


class FakeDispatcher:
    def __init__(self):
        self.reject_next_payout = False
        self.sent = []
        self.completed = []
        self.payout_hold = None

    async def send(self, subsystem, command, params=None):
        self.sent.append((command, params, None))
        return CommandAck(request_id="r" * 8, command=command, status="ok")

    async def send_and_await_completion(
        self, subsystem, command, params=None, request_id=None
    ):
        self.sent.append((command, params, request_id))
        if command == "payout" and self.reject_next_payout:
            self.reject_next_payout = False
            return CommandAck(
                request_id=request_id,
                command=command,
                status="rejected",
                detail="no_policy",
            )
        return CommandAck(
            request_id=request_id,
            command=command,
            status="ok",
            detail=None,
            result={
                "kind": "payout_result",
                "request_id": request_id,
                "requested_cents": (params or {}).get("cents", 1),
                "paid_cents": (params or {}).get("cents", 1),
                "status": "ok",
            },
        )

    def complete(self, request_id, ack):
        self.completed.append((request_id, ack))


class Env:
    def __init__(self, tmp_path):
        self.cfg = MdbPolicyConfig()
        self.disp = FakeDispatcher()
        self.runner = FakeTaskRunner()
        self.runner.attach(asyncio.get_running_loop())
        self.faults = FakeFaults()
        self.paid = []
        self.edge_cash = 0
        self.acks = []
        self.gw = MdbGateway(
            dispatcher=self.disp,
            config=lambda: self.cfg,
            payment_enabled=lambda: True,
            tasks=self.runner,
            faults=self.faults,
            board_alive=lambda: True,
            availability=lambda: None,
            deposit=lambda *a: None,
            persist_session=self._noop,
            on_lever=lambda: None,
            journal=lambda *a: None,
            record_event=lambda *a: None,
            on_paid=lambda item, cents: self.paid.append((item, cents)),
            on_changed=lambda: None,
            on_cursor_saved=lambda ok: None,
            fsm_idle=lambda: True,
            pay104_active=lambda: False,
            edge_cash_escrow=lambda boot: self.edge_cash,
            publish_ack=self.acks.append,
            cursor=PaymentCursorStore(tmp_path / "cur.json"),
        )

    @staticmethod
    async def _noop():
        return None

    def policy_id(self):
        return build_policy(self.cfg, True).policy_id

    async def ack_policy(self):
        self.gw.policy.on_connected()
        await settle()
        await self.gw.state_consumer.handle("s", doc(self.policy_id()))
        await settle()


@pytest.fixture
async def env(tmp_path):
    return Env(tmp_path)


async def test_construct_exposes_units(env):
    gw = env.gw
    assert gw.state is None and gw.policy_acked is False
    assert gw.intake and gw.policy and gw.state_consumer
    assert gw.payouts and gw.reconciler


async def test_pay_out_and_cash_sale_delegate_after_policy_acked(env):
    env.gw.pay_out(100, PayoutReason.change, "s1")
    env.gw.cash_sale(200, "s1")
    await settle()
    assert [c for c, *_ in env.disp.sent if c in ("payout", "cash_sale")] == []
    assert [p.kind for p in env.gw.pending_payouts()] == ["payout", "cash_sale"]
    await env.ack_policy()
    assert env.gw.policy_acked
    await settle(10)
    cmds = [c for c, *_ in env.disp.sent if c in ("payout", "cash_sale")]
    assert cmds == ["payout", "cash_sale"]
    assert env.gw.pending_payouts() == []


async def test_payout_send_defers_keepalive(env):
    await env.ack_policy()
    env.gw.policy.on_connected()
    before = [c for c in env.runner.scheduled if c.label == "mdb_keepalive"]
    assert len(before) == 1
    assert before[0].delay == EDGE_IDLE_PING_SECONDS
    env.gw.pay_out(100, PayoutReason.cancel, None)
    await settle(6)
    live = [c for c in env.runner.scheduled if c.label == "mdb_keepalive"]
    assert before[0].task.cancelled  # the send reset the idle-ping clock
    assert len(live) == 1 and live[0] is not before[0]


async def test_payout_result_event_completes_via_dispatcher(env):
    ev = {
        "event_id": "evt-boot-0001-0001",
        "boot_id": BOOT,
        "seq": 1,
        "ts": "2026-10-30T12:00:00Z",
        "event": {
            "kind": "payout_result",
            "request_id": "q" * 12,
            "requested_cents": 100,
            "paid_cents": 100,
            "status": "ok",
        },
    }
    await env.gw.intake.handle("payment/event", ev)
    ((rid, ack),) = env.disp.completed
    assert rid == "q" * 12
    assert ack.command == "payout" and ack.status == "ok" and ack.detail == "ok"
    assert ack.result["paid_cents"] == 100
    assert env.acks


async def test_reconciler_pays_out_board_excess_via_state(env):
    await env.ack_policy()
    env.gw.reconciler.arm("test")
    await env.gw.state_consumer.handle("s", doc(env.policy_id(), escrow=75))
    await settle(10)
    payouts = [p for c, p, _ in env.disp.sent if c == "payout"]
    assert payouts and payouts[0]["cents"] == 75 and payouts[0]["reason"] == "error"


async def test_no_policy_rejection_resyncs_policy_then_pays_with_fresh_id(env):
    await env.ack_policy()
    assert env.gw.policy_acked
    env.disp.reject_next_payout = True
    env.gw.pay_out(100, PayoutReason.cancel, None)
    await settle(8)
    # rejection dropped the ack and re-sent the policy, but the stale retained
    # state must not re-ack it: the false window holds and nothing is re-sent
    assert len([c for c, *_ in env.disp.sent if c == "set_policy"]) == 2
    assert not env.gw.policy_acked
    assert len([c for c, *_ in env.disp.sent if c == "payout"]) == 1
    assert env.paid == []
    # a fresh state echoing the policy re-acks and releases the payout
    await env.gw.state_consumer.handle("s", doc(env.policy_id()))
    await settle(8)
    assert env.gw.policy_acked
    payouts = [r for c, _, r in env.disp.sent if c == "payout"]
    assert len(payouts) == 2 and payouts[0] != payouts[1]
    assert env.paid and env.paid[0][1] == 100
    assert env.gw.pending_payouts() == []
