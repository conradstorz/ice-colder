"""`Machine` wiring of the MDB gateway: DATA-104 sources, boot-time intake
seeding, the snapshot lifecycle around a payout, policy sync and the lever."""

import asyncio
import json

from ice_common.config_model import ConfigModel, Product
from ice_contracts.board.common import CommandAck
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from ice_edge.faults.availability import Availability
from ice_edge.ledger.payment_cursor import PaymentCursorStore
from ice_edge.ledger.session_store import Credit, SessionSnapshot, SessionStore
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.payment.mdb.policy import build_policy
from tests.fakes import settle

BOOT = "boot-9a-0001"


def event(seq, kind, cents, boot=BOOT, **extra):
    body = {"kind": kind, **extra}
    if kind != "escrow_return_pressed":
        body["cents"] = cents
    if kind == "coin_accepted":
        body.setdefault("routed", "tubes")
    return {
        "event_id": f"evt-{boot}-{seq:04d}",
        "boot_id": boot,
        "seq": seq,
        "ts": "2026-10-30T12:00:00Z",
        "event": body,
    }


def state_doc(policy_id, boot=BOOT, seq_last=0):
    return {
        "boot_id": boot,
        "seq_last": seq_last,
        "policy_id": policy_id,
        "accepting": True,
        "inhibited_reason": None,
        "escrow_cents": 0,
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


class BoardDispatcher:
    """Stands in for the CommandDispatcher: records sends, pays every payout in full."""

    def __init__(self):
        self.sent: list[tuple[str, dict, str | None]] = []

    async def send(self, subsystem, command, params=None, request_id=None):
        self.sent.append((command, params or {}, request_id))
        return CommandAck(
            request_id=request_id or "r" * 8, command=command, status="ok"
        )

    async def send_and_await_completion(
        self, subsystem, command, params=None, request_id=None
    ):
        self.sent.append((command, params or {}, request_id))
        cents = (params or {}).get("cents", 1)
        return CommandAck(
            request_id=request_id,
            command=command,
            status="ok",
            result={
                "kind": "payout_result",
                "request_id": request_id,
                "requested_cents": cents,
                "paid_cents": cents,
                "status": "ok",
            },
        )

    def complete(self, request_id, ack):
        pass

    def commands(self, name):
        return [c for c in self.sent if c[0] == name]


class FailingStore(SessionStore):
    """A session store whose snapshot writes can be made to fail."""

    def __init__(self, path):
        super().__init__(path)
        self.fail = False

    async def save_async(self, snap):
        if self.fail:
            raise OSError("disk full")
        await super().save_async(snap)


class FailingCursor(PaymentCursorStore):
    def __init__(self, path):
        super().__init__(path)
        self.fail = False

    def save(self, state):
        if self.fail:
            raise OSError("disk full")
        super().save(state)


def build(tmp_path, *, store=None, cursor=None, with_board=True):
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="ICE-1", name="Ice", price_cents=250, slot=0, kind="ice")
    ]
    machine = Machine(
        cfg, payment_cursor_store=cursor or PaymentCursorStore(tmp_path / "cur.json")
    )
    machine.attach_to_loop(asyncio.get_running_loop())
    monitor = HealthMonitor()
    machine.set_health_monitor(monitor)
    avail = Availability()
    machine.set_availability(avail)
    dispatcher = BoardDispatcher()
    machine.set_command_dispatcher(dispatcher)
    if store is not None:
        machine.set_session_store(store)
    if with_board:
        monitor.record_heartbeat("mdb", {"uptime_seconds": 1})
    return machine, cfg, avail, dispatcher


async def ack_policy(machine, cfg, avail):
    machine.on_mqtt_connection(True)
    await settle(6)
    policy_id = build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    await machine.gateway.state_consumer.handle("payment/state", state_doc(policy_id))
    await settle(6)
    assert machine.gateway.policy_acked


# --- DATA-104 has two failure sources -----------------------------------------


async def test_data104_not_cleared_by_cursor_success_while_snapshot_fails(tmp_path):
    store = FailingStore(tmp_path / "session.json")
    machine, *_ = build(tmp_path, store=store)
    store.fail = True
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")  # snapshot failed, cursor saved fine
    await machine.gateway.intake.handle("payment/event", event(2, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")


async def test_data104_not_cleared_by_snapshot_success_while_cursor_fails(tmp_path):
    cursor = FailingCursor(tmp_path / "cur.json")
    store = FailingStore(tmp_path / "session.json")
    machine, *_ = build(tmp_path, store=store, cursor=cursor)
    cursor.fail = True
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")
    await settle(4)  # the deposit's own snapshot write succeeded meanwhile
    await machine.gateway.intake.handle("payment/event", event(2, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")


async def test_data104_clears_only_when_both_sources_recover(tmp_path):
    cursor = FailingCursor(tmp_path / "cur.json")
    store = FailingStore(tmp_path / "session.json")
    machine, *_ = build(tmp_path, store=store, cursor=cursor)
    cursor.fail = True
    store.fail = True
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")

    store.fail = False  # snapshot recovers, cursor still failing
    await machine.gateway.intake.handle("payment/event", event(2, "bill_stacked", 100))
    await settle(4)
    assert machine.faults.has("DATA-104")

    cursor.fail = False  # now the cursor recovers too
    await machine.gateway.intake.handle("payment/event", event(3, "bill_stacked", 100))
    await settle(4)
    assert not machine.faults.has("DATA-104")


async def test_data104_cleared_when_the_only_failing_source_recovers(tmp_path):
    cursor = FailingCursor(tmp_path / "cur.json")
    machine, *_ = build(tmp_path, cursor=cursor)
    cursor.fail = True
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 100))
    assert machine.faults.has("DATA-104")
    cursor.fail = False
    await machine.gateway.intake.handle("payment/event", event(2, "bill_stacked", 100))
    assert not machine.faults.has("DATA-104")


# --- deposits carry their event id; boot seeds the intake from the snapshot --------


async def test_deposit_from_intake_records_event_id_on_the_credit(tmp_path):
    machine, *_ = build(tmp_path)
    await machine.gateway.intake.handle("payment/event", event(1, "coin_accepted", 25))
    (credit,) = machine.escrow.credits
    assert (credit.method, credit.cents, credit.event_id) == (
        "cash_coin",
        25,
        f"evt-{BOOT}-0001",
    )


async def test_boot_seeds_dedup_from_snapshot_credit_event_ids(tmp_path):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(
            state="interacting_with_user",
            credit_escrow_cents=500,
            credits=[Credit("cash_bill", 500, 1.0, event_id=f"evt-{BOOT}-0001")],
        )
    )
    machine, *_ = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has("PAY-104")  # the open snapshot is still evidence

    # The board replays the event the crashed edge had already persisted.
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))

    assert machine.escrow.total_cents == 0  # not credited a second time


# --- snapshot lifecycle around a payout ---------------------------------------------


async def test_session_file_survives_while_payout_pending_and_goes_after_payout(
    tmp_path,
):
    path = tmp_path / "session.json"
    machine, cfg, avail, dispatcher = build(tmp_path, store=SessionStore(path))
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))
    await settle(6)
    assert path.exists()  # credit in escrow

    machine.vmc.request_refund(reason="customer")
    await settle(6)
    await machine.drain_persistence()

    assert machine.gateway.pending_payouts()  # board not ready: still owed
    saved = json.loads(path.read_text())
    assert saved["pending_payouts"][0]["cents"] == 500  # evidence survives a crash
    assert saved["pending_payouts"][0]["kind"] == "payout"

    await ack_policy(machine, cfg, avail)
    await settle(10)
    await machine.drain_persistence()

    assert [c[1]["cents"] for c in dispatcher.commands("payout")] == [500]
    assert machine.gateway.pending_payouts() == []
    assert not path.exists()  # paid: no spurious PAY-104 at the next boot


async def test_next_boot_with_unpaid_payout_raises_pay104(tmp_path):
    path = tmp_path / "session.json"
    machine, *_ = build(tmp_path, store=SessionStore(path))
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))
    machine.vmc.request_refund(reason="customer")
    await settle(6)
    await machine.drain_persistence()

    rebooted, *_ = build(tmp_path, store=SessionStore(path))

    assert rebooted.faults.has("PAY-104")


# --- policy sync, liveness and the lever -------------------------------------------


async def test_mqtt_connect_sends_policy_and_availability_change_resyncs(tmp_path):
    machine, cfg, avail, dispatcher = build(tmp_path)
    machine.on_mqtt_connection(True)
    await settle(6)
    first = dispatcher.commands("set_policy")
    assert first and first[-1][1]["accept"] is True

    machine.vmc.raise_fault(FaultCode.WTR_104, outcome="leak")
    await settle(6)
    assert dispatcher.commands("set_policy")[-1][1]["accept"] is False


async def test_payout_is_held_until_the_mdb_board_is_alive(tmp_path):
    machine, cfg, avail, dispatcher = build(tmp_path, with_board=False)
    await ack_policy_without_board(machine, cfg, avail)
    machine.vmc.deposit_funds(100, "cash_coin")
    machine.vmc.request_refund(reason="customer")
    await settle(8)
    assert dispatcher.commands("payout") == []
    assert len(machine.gateway.pending_payouts()) == 1

    # The board's first heartbeat arrives after the policy was acked, with no
    # PAY-101 ever raised: the queued payout must go out now.
    machine.health_monitor.record_heartbeat("mdb", {"uptime_seconds": 1})
    await settle(10)

    assert len(dispatcher.commands("payout")) == 1
    assert machine.gateway.pending_payouts() == []


async def ack_policy_without_board(machine, cfg, avail):
    machine.on_mqtt_connection(True)
    await settle(6)
    policy_id = build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    await machine.gateway.state_consumer.handle("payment/state", state_doc(policy_id))
    await settle(6)


async def test_lever_event_refunds_credit_and_is_ignored_while_dispensing(tmp_path):
    machine, *_ = build(tmp_path)
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))
    await machine.gateway.intake.handle(
        "payment/event", event(2, "escrow_return_pressed", 0)
    )
    (payout,) = machine.gateway.pending_payouts()
    assert (payout.cents, payout.reason) == (500, "customer")

    (tmp_path / "second").mkdir()
    machine2, *_ = build(tmp_path / "second")
    await machine2.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))
    machine2.vmc.set_state("dispensing")
    await machine2.gateway.intake.handle(
        "payment/event", event(2, "escrow_return_pressed", 0)
    )
    assert machine2.gateway.pending_payouts() == []
    assert machine2.escrow.total_cents == 500
