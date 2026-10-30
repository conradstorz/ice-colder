"""`Machine` wiring of the MDB gateway: DATA-104 sources, boot-time intake
seeding, the snapshot lifecycle around a payout, policy sync and the lever."""

import asyncio
import itertools
import json
import types

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
from tests.payment_state_helpers import set_coin_status

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


def build(
    tmp_path, *, store=None, cursor=None, with_board=True, attach_availability=True
):
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


# --- change at once: the snapshot lifecycle of an overpaid vend ---------------------


async def test_overpay_vend_payout_ok_removes_session_json(tmp_path):
    from tests.dispenser_fixtures import profiles_for

    path = tmp_path / "session.json"
    machine, cfg, avail, dispatcher = build(tmp_path, store=SessionStore(path))
    machine.set_dispenser_profiles(profiles_for(cfg.physical.products, tmp_path))
    for name in ("vending", "ice_maker"):
        machine.health_monitor.record_heartbeat(name, {"uptime_seconds": 1})
    set_coin_status(avail, "ready")
    await machine.telemetry.handle_hardware_io(
        "hardware/io/bin_half_full", {"device": "bin_half_full", "state": True}
    )
    await ack_policy(machine, cfg, avail)
    vmc = machine.vmc
    await machine.gateway.intake.handle("payment/event", event(1, "bill_stacked", 500))
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.process_payment()
    await settle(6)
    assert vmc.state == "dispensing"
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "complete"})
    await settle(12)
    await machine.drain_persistence()

    assert vmc.state == "idle"
    # change 250 paid, the sale's 250 reported as cash_sale, both confirmed ok
    assert [c[1]["cents"] for c in dispatcher.commands("payout")] == [250]
    assert [c[1]["cents"] for c in dispatcher.commands("cash_sale")] == [250]
    assert machine.gateway.pending_payouts() == []
    assert not path.exists()  # no stale open snapshot: no spurious PAY-104 next boot


# --- PAY-104 recovery drives the board ------------------------------------------------


def _pay104_snapshot(tmp_path):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(
            state="idle",
            credit_escrow_cents=100,
            credits=[Credit(method="cash_coin", cents=100, ts=1.0)],
            selected_sku="ICE-1",
            dispense_slot=0,
            pending_sale_shares={"cash_bill": 200, "card": 50},
        )
    )
    return path


async def test_record_as_sale_sends_cash_share_then_reconciliation_pays_only_leftover(
    tmp_path, monkeypatch
):
    clock = itertools.count(1000.0, 1.0)
    fake_time = types.SimpleNamespace(time=lambda: next(clock))
    monkeypatch.setattr("ice_edge.payment.mdb.payout.time", fake_time)
    monkeypatch.setattr("ice_edge.payment.mdb.state.time", fake_time)
    path = _pay104_snapshot(tmp_path)
    machine, cfg, avail, dispatcher = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has(FaultCode.PAY_104)
    await ack_policy(machine, cfg, avail)
    pending = machine.pending_sale_for_recovery()
    assert pending is not None

    # The board still holds the sale's 200 of cash plus the 100 leftover credit.
    policy_id = build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    stale = state_doc(policy_id)
    stale["escrow_cents"] = 300
    await machine.gateway.state_consumer.handle("payment/state", stale)
    assert machine.send_recovered_cash_sale(pending) is True
    assert machine.send_recovered_cash_sale(pending) is False  # once per sale
    await settle(10)
    assert [
        (c[1]["cents"], c[1].get("sale_id")) for c in dispatcher.commands("cash_sale")
    ] == [(200, None)]

    machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin")
    board = state_doc(
        build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    )
    board["escrow_cents"] = 100  # after the cash_sale: only the leftover remains
    await machine.gateway.state_consumer.handle("payment/state", board)
    await settle(10)

    assert [(c[1]["cents"], c[1]["reason"]) for c in dispatcher.commands("payout")] == [
        (100, "error")
    ]


async def test_discard_sends_nothing_for_the_sale(tmp_path):
    path = _pay104_snapshot(tmp_path)
    machine, cfg, avail, dispatcher = build(tmp_path, store=SessionStore(path))
    await ack_policy(machine, cfg, avail)
    machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin")
    await settle(10)
    assert dispatcher.commands("cash_sale") == []


async def test_event_ack_without_a_loop_creates_no_coroutine(tmp_path, recwarn):
    from ice_contracts.board.payment import PaymentEventAck
    from tests.fakes import FakeMqtt

    machine, *_ = build(tmp_path)
    machine.set_mqtt_client(FakeMqtt())
    machine.tasks.loop = None
    # private: the ack publisher is wired into the intake, with no public seam
    machine._publish_event_ack(PaymentEventAck(event_id="e", boot_id=BOOT, seq=1))
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


async def test_legacy_refund_id_appears_in_the_pay104_detail(tmp_path, caplog):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(
            state="idle", credit_escrow_cents=0, pending_refund_request_id="legacy-7"
        )
    )
    machine, *_ = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has(FaultCode.PAY_104)
    assert "refund=legacy-7" in caplog.text


# --- PAY-104 resolution re-queues what the crashed run left unconfirmed ---------------


def _owed_snapshot(tmp_path, items, **extra):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(
            state="idle", credit_escrow_cents=0, pending_payouts=items, **extra
        )
    )
    return path


async def _resolved(tmp_path, items, resolve, board_escrow, **extra):
    path = _owed_snapshot(tmp_path, items, **extra)
    machine, cfg, avail, dispatcher = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has(FaultCode.PAY_104)
    await ack_policy(machine, cfg, avail)
    stale = state_doc(
        build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    )
    stale["escrow_cents"] = board_escrow
    await machine.gateway.state_consumer.handle("payment/state", stale)
    # Nothing is re-sent while PAY-104 is active.
    await settle(8)
    assert (
        dispatcher.commands("cash_sale") == [] and dispatcher.commands("payout") == []
    )
    resolve(machine)
    await settle(12)
    return machine, cfg, avail, dispatcher, stale


async def test_discard_requeues_a_pending_cash_sale_and_pays_no_error_payout(tmp_path):
    items = [
        {
            "request_id": "c" * 32,
            "kind": "cash_sale",
            "cents": 250,
            "reason": None,
            "sale_id": "s1",
        }
    ]
    machine, cfg, avail, dispatcher, stale = await _resolved(
        tmp_path,
        items,
        lambda m: m.vmc.clear_fault(FaultCode.PAY_104.value, by="admin"),
        board_escrow=250,
    )
    sent = dispatcher.commands("cash_sale")
    assert [(c[1]["cents"], c[2]) for c in sent] == [(250, "c" * 32)]
    assert dispatcher.commands("payout") == []
    fresh = dict(stale, escrow_cents=0)
    await machine.gateway.state_consumer.handle("payment/state", fresh)
    await settle(8)
    assert dispatcher.commands("payout") == []


async def test_clear_requeues_a_pending_change_payout_and_pays_nothing_extra(tmp_path):
    items = [
        {
            "request_id": "p" * 32,
            "kind": "payout",
            "cents": 100,
            "reason": "change",
            "sale_id": "s1",
        }
    ]
    machine, cfg, avail, dispatcher, stale = await _resolved(
        tmp_path,
        items,
        lambda m: m.vmc.clear_fault(FaultCode.PAY_104.value, by="admin"),
        board_escrow=100,
    )
    sent = dispatcher.commands("payout")
    assert [(c[1]["cents"], c[1]["reason"], c[2]) for c in sent] == [
        (100, "change", "p" * 32)
    ]
    await machine.gateway.state_consumer.handle(
        "payment/state", dict(stale, escrow_cents=0)
    )
    await settle(8)
    assert len(dispatcher.commands("payout")) == 1


async def test_record_as_sale_sends_its_cash_sale_besides_an_unrelated_queued_one(
    tmp_path,
):
    # An earlier sale's unconfirmed cash_sale of equal cents must not suppress
    # the recovered sale's own report.
    items = [
        {
            "request_id": "c" * 32,
            "kind": "cash_sale",
            "cents": 200,
            "reason": None,
            "sale_id": "s0",
        }
    ]

    def record(machine):
        pending = machine.pending_sale_for_recovery()
        assert pending is not None
        assert machine.send_recovered_cash_sale(pending) is True
        machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin")

    machine, cfg, avail, dispatcher, _ = await _resolved(
        tmp_path,
        items,
        record,
        board_escrow=400,
        selected_sku="ICE-1",
        dispense_slot=0,
        pending_sale_shares={"cash_bill": 200, "card": 50},
    )
    sent = dispatcher.commands("cash_sale")
    assert [c[1]["cents"] for c in sent] == [200, 200]
    ids = [c[2] for c in sent]
    assert "c" * 32 in ids and len(set(ids)) == 2
