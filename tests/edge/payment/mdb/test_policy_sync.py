import asyncio

import pytest

from ice_common.config_model import MdbPolicyConfig
from ice_contracts.board.common import CommandAck
from ice_contracts.board.payment import EDGE_IDLE_PING_SECONDS, PaymentState
from ice_edge.hal.command_dispatcher import CommandTimeout
from ice_edge.payment.mdb.policy import PolicySync, build_policy
from tests.fakes import FakeTaskRunner, settle

BOOT = "boot-0001"


def ack(status="ok", detail="queued"):
    return CommandAck(
        request_id="r" * 8, command="set_policy", status=status, detail=detail
    )


class FakeDispatcher:
    def __init__(self):
        self.sent = []
        self.replies = []  # items: CommandAck | Exception | asyncio.Event gate
        self.gate = None

    async def send(self, subsystem, command, params=None):
        self.sent.append((subsystem, command, params))
        if self.gate is not None:
            await self.gate.wait()
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return ack()

    def policies(self):
        return [p for _, c, p in self.sent if c == "set_policy"]

    def pings(self):
        return [c for _, c, _ in self.sent if c == "ping"]


def state(policy_id, boot=BOOT):
    return PaymentState(
        boot_id=boot,
        seq_last=0,
        policy_id=policy_id,
        accepting=True,
        inhibited_reason=None,
        escrow_cents=0,
        bill_in_escrow_cents=None,
        devices={
            "coin": {"present": True, "status": "ready"},
            "bill": {"present": True, "status": "ready"},
            "cashless": {"present": False, "status": "offline"},
        },
        tubes=[],
        exact_change=False,
        stacker=None,
        session=None,
    )


class Env:
    def __init__(self):
        self.cfg = MdbPolicyConfig()
        self.enabled = True
        self.alive = True
        self.disp = FakeDispatcher()
        self.runner = FakeTaskRunner()
        self.runner.attach(asyncio.get_running_loop())
        self.sync = PolicySync(
            dispatcher=self.disp,
            config=lambda: self.cfg,
            payment_enabled=lambda: self.enabled,
            tasks=self.runner,
            board_alive=lambda: self.alive,
        )

    def cur_id(self):
        return build_policy(self.cfg, self.enabled).policy_id


@pytest.fixture
async def env():
    return Env()


def test_policy_id_deterministic_and_sensitive():
    cfg = MdbPolicyConfig()
    a = build_policy(cfg, True)
    assert a == build_policy(MdbPolicyConfig(), True)
    assert len(a.policy_id) == 16
    assert a.cashless_enabled is False
    ids = {a.policy_id, build_policy(cfg, False).policy_id}
    for change in (
        {"coin_enabled": [5]},
        {"bill_enabled": [100]},
        {"max_escrow_cents": 1500},
        {"change_threshold_cents": 100},
        {"edge_timeout_s": 40},
        {"return_bill_on_lever": False},
    ):
        ids.add(build_policy(cfg.model_copy(update=change), True).policy_id)
    assert len(ids) == 8


async def test_accept_follows_payment_enabled(env):
    env.enabled = False
    await env.sync.sync("t")
    assert env.disp.policies()[-1]["accept"] is False
    env.enabled = True
    env.sync.on_payment_enabled_changed(True)
    await settle()
    assert env.disp.policies()[-1]["accept"] is True


async def test_sends_on_connect_and_flip(env):
    env.sync.on_connected()
    await settle()
    assert len(env.disp.policies()) == 1
    env.enabled = False
    env.sync.on_payment_enabled_changed(False)
    await settle()
    assert len(env.disp.policies()) == 2


async def test_queued_ack_alone_not_acked_then_echo(env):
    await env.sync.sync("t")
    assert env.sync.policy_acked is False
    events = []
    env.sync.subscribe_policy_acked(events.append)
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True
    assert events == [True]
    assert len(env.disp.policies()) == 1


async def test_echo_before_ack_does_not_count(env):
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is False


async def test_resend_on_null_policy_id(env):
    env.sync.on_state(state(None))
    await settle()
    assert len(env.disp.policies()) == 1


async def test_resend_on_different_policy_id(env):
    env.sync.on_state(state("deadbeefdeadbeef"))
    await settle()
    assert len(env.disp.policies()) == 1


async def test_echo_before_verify_timer_no_resend(env):
    await env.sync.sync("t")
    assert [c.label for c in env.runner.scheduled] == ["policy_verify"]
    env.sync.on_state(state(env.cur_id()))
    assert env.runner.scheduled == []
    assert len(env.disp.policies()) == 1


async def test_verify_timer_resends_once(env):
    await env.sync.sync("t")
    env.sync.on_state(state(None))  # a non-echo does not resend by itself
    await settle()
    assert len(env.disp.policies()) == 1
    env.runner.fire("policy_verify")
    await settle()
    assert len(env.disp.policies()) == 2
    assert env.sync.policy_acked is False


async def test_payment_flip_drops_acked_immediately(env):
    await env.sync.sync("t")
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True
    old = env.cur_id()
    env.enabled = False
    env.sync.on_payment_enabled_changed(False)
    assert env.sync.policy_acked is False
    await settle()
    env.sync.on_state(state(old))
    assert env.sync.policy_acked is False
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True


async def test_no_state_triggered_send_while_retry_pending(env):
    env.disp.replies = [ack("rejected", None)]
    await env.sync.sync("t")
    assert [c.label for c in env.runner.scheduled] == ["policy_retry"]
    env.sync.on_state(state(None))
    await settle()
    assert len(env.disp.policies()) == 1


async def test_syncs_serialised_and_stale_ack_ignored(env):
    env.disp.gate = asyncio.Event()
    first = asyncio.ensure_future(env.sync.sync("a"))
    await settle()
    old = env.cur_id()
    env.enabled = False
    await env.sync.sync("b")  # folded into one re-send after the first finishes
    assert len(env.disp.policies()) == 1
    env.disp.gate.set()
    await first
    assert len(env.disp.policies()) == 2
    env.sync.on_state(state(old))
    assert env.sync.policy_acked is False
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True


async def test_new_boot_resends_and_drops_acked(env):
    await env.sync.sync("t")
    env.sync.on_state(state(env.cur_id()))
    events = []
    env.sync.subscribe_policy_acked(events.append)
    env.sync.on_state(state(None, boot="boot-0002"))
    assert events == [False]
    await settle()
    assert len(env.disp.policies()) == 2
    env.sync.on_state(state(env.cur_id(), boot="boot-0002"))
    assert events == [False, True]


async def test_older_boot_echo_does_not_count(env):
    env.sync.on_state(state(None))
    await settle()  # sends, ok-acked at BOOT
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True
    env.sync.on_state(state(env.cur_id(), boot="boot-OLD1"))
    assert env.sync.policy_acked is False
    await settle()
    assert len(env.disp.policies()) == 2


async def test_timeout_retries_only_while_alive(env):
    env.disp.replies = [CommandTimeout("mdb", "set_policy")]
    await env.sync.sync("t")
    assert [c.label for c in env.runner.scheduled] == ["policy_retry"]
    env.runner.fire("policy_retry")
    await settle()
    assert len(env.disp.policies()) == 2
    assert [c.label for c in env.runner.scheduled] == ["policy_verify"]

    env.alive = False
    env.disp.replies = [ack("rejected", None)]
    await env.sync.sync("t")
    assert env.runner.scheduled == []
    assert env.sync.policy_acked is False


async def test_non_ok_ack_retries(env):
    env.disp.replies = [ack("failed", "x")]
    await env.sync.sync("t")
    assert [c.label for c in env.runner.scheduled] == ["policy_retry"]


async def test_keepalive_pings_after_silence(env):
    env.sync.on_connected()
    await settle()
    cals = [c for c in env.runner.scheduled if c.label == "mdb_keepalive"]
    assert len(cals) == 1 and cals[0].delay == EDGE_IDLE_PING_SECONDS
    env.runner.fire("mdb_keepalive")
    await settle()
    assert env.disp.pings() == ["ping"]
    assert [c.label for c in env.runner.scheduled].count("mdb_keepalive") == 1


async def test_keepalive_not_while_disconnected(env):
    env.sync.on_connected()
    await settle()
    env.sync.on_disconnected()
    assert [c for c in env.runner.scheduled if c.label == "mdb_keepalive"] == []


async def test_any_send_resets_keepalive(env):
    env.sync.on_connected()
    await settle()
    first = [c for c in env.runner.scheduled if c.label == "mdb_keepalive"][0]
    env.sync.note_command_sent()
    assert first.task.done()
    live = [c for c in env.runner.scheduled if c.label == "mdb_keepalive"]
    assert len(live) == 1 and live[0] is not first


async def test_at_most_one_ping_in_flight(env):
    env.sync.on_connected()
    await settle()
    env.disp.gate = asyncio.Event()
    env.runner.fire("mdb_keepalive")
    await settle()
    assert len(env.disp.pings()) == 1
    env.runner.fire("mdb_keepalive")
    await settle()
    assert len(env.disp.pings()) == 1
    env.disp.gate.set()
    await settle()
    env.disp.gate = None
    env.runner.fire("mdb_keepalive")
    await settle()
    assert len(env.disp.pings()) == 2


async def test_null_policy_after_echo_same_boot_resends(env):
    await env.sync.sync("t")
    env.sync.on_state(state(env.cur_id()))
    assert env.sync.policy_acked is True
    env.sync.on_state(state(None))
    assert env.sync.policy_acked is False
    await settle()
    assert len(env.disp.policies()) == 2


async def test_null_state_during_identical_inflight_send_no_second_send(env):
    env.disp.gate = asyncio.Event()
    first = asyncio.ensure_future(env.sync.sync("a"))
    await settle()
    env.sync.on_state(state(None))
    await settle()
    env.disp.gate.set()
    await first
    assert len(env.disp.policies()) == 1
