"""PAY-104 and the session file: only held evidence freezes persistence.

A runtime PAY-104 (escrow mismatch, cash_sale outcome) must not stop the live
session from being saved, and clearing it must not delete the live file. An
uncertain boot snapshot is evidence and stays frozen until resolved."""

import json

from ice_contracts.board.vending_machine import FaultCode
from ice_edge.ledger.session_store import Credit, SessionSnapshot, SessionStore
from tests.edge.app.test_machine_payments import BOOT, build, event
from tests.fakes import settle


async def test_runtime_pay104_does_not_stop_session_persistence(tmp_path):
    path = tmp_path / "session.json"
    machine, *_ = build(tmp_path, store=SessionStore(path))
    machine.vmc.raise_fault(FaultCode.PAY_104, outcome="escrow_mismatch:0/100")

    await machine.gateway.intake.handle("payment/event", event(1, "coin_accepted", 25))
    await settle(6)
    await machine.drain_persistence()

    assert path.exists()
    assert json.loads(path.read_text())["credit_escrow_cents"] == 25


async def test_clearing_a_runtime_pay104_keeps_the_live_session_file(tmp_path):
    path = tmp_path / "session.json"
    machine, *_ = build(tmp_path, store=SessionStore(path))
    machine.vmc.raise_fault(FaultCode.PAY_104, outcome="escrow_mismatch:0/100")
    await machine.gateway.intake.handle("payment/event", event(1, "coin_accepted", 25))
    await settle(6)
    await machine.drain_persistence()
    assert path.exists()

    assert machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin") is True

    assert path.exists()  # the customer's live credit is still on disk
    assert not machine.faults.has(FaultCode.PAY_104)


async def test_boot_pay104_freezes_evidence_until_resolved(tmp_path):
    path = tmp_path / "session.json"
    SessionStore(path).save(
        SessionSnapshot(
            state="interacting_with_user",
            credit_escrow_cents=500,
            credits=[Credit("cash_bill", 500, 1.0, event_id=f"{BOOT}-0001")],
        )
    )
    before = path.read_text()
    machine, *_ = build(tmp_path, store=SessionStore(path))
    assert machine.faults.has(FaultCode.PAY_104)

    await machine.gateway.intake.handle("payment/event", event(2, "coin_accepted", 25))
    await settle(6)
    await machine.drain_persistence()
    assert path.read_text() == before  # frozen

    assert machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin") is True
    assert not path.exists()  # evidence removed on resolution

    # and the hold is released: the next deposit persists again
    await machine.gateway.intake.handle("payment/event", event(3, "coin_accepted", 10))
    await settle(6)
    await machine.drain_persistence()
    assert path.exists()


# --- boot recovery of a recorded sale's cash_sale ----------------------------------


class RecordedSaleRecorder:
    """An event recorder whose `sales` table already holds the crashed sale."""

    def record(self, *a, **k):
        pass

    def sale_recorded_since(self, *a, **k):
        return True


def _recorded_sale_snapshot(path, *, pending_payouts=()):
    SessionStore(path).save(
        SessionSnapshot(
            state="idle",
            credit_escrow_cents=0,
            selected_sku="ICE-1",
            dispense_slot=0,
            pending_sale_shares={"cash_bill": 250},
            pending_payouts=list(pending_payouts),
        )
    )


def _boot_with_recorder(tmp_path, path):
    from tests.edge.app.test_machine_payments import build as _build

    machine, cfg, avail, dispatcher = _build(tmp_path)
    machine.set_event_recorder(RecordedSaleRecorder())
    machine.set_session_store(SessionStore(path))
    return machine, cfg, avail, dispatcher


async def test_boot_queues_the_cash_sale_of_an_already_recorded_sale(tmp_path):
    from ice_edge.payment.mdb.policy import build_policy
    from tests.edge.app.test_machine_payments import ack_policy, state_doc

    path = tmp_path / "session.json"
    _recorded_sale_snapshot(path)
    machine, cfg, avail, dispatcher = _boot_with_recorder(tmp_path, path)
    assert not machine.faults.has(FaultCode.PAY_104)  # closed, not uncertain
    assert [p.kind for p in machine.gateway.pending_payouts()] == ["cash_sale"]

    # The board still holds the sale's 250: edge-start reconciliation must
    # find the cash_sale queued and pay nothing out.
    await ack_policy(machine, cfg, avail)
    policy_id = build_policy(cfg.payment.mdb.policy, avail.payment_enabled).policy_id
    doc = state_doc(policy_id)
    doc["escrow_cents"] = 250
    await machine.gateway.state_consumer.handle("payment/state", doc)
    await settle(12)

    assert [c[1]["cents"] for c in dispatcher.commands("cash_sale")] == [250]
    assert dispatcher.commands("payout") == []
    assert not machine.faults.has(FaultCode.PAY_104)


async def test_boot_does_not_double_queue_a_snapshot_cash_sale(tmp_path):
    from tests.edge.app.test_machine_payments import ack_policy

    path = tmp_path / "session.json"
    _recorded_sale_snapshot(
        path,
        pending_payouts=[
            {"request_id": "cs1", "kind": "cash_sale", "cents": 250, "sale_id": None}
        ],
    )
    machine, cfg, avail, dispatcher = _boot_with_recorder(tmp_path, path)
    # A pending payout is evidence: uncertain, nothing queued by the boot path.
    assert machine.faults.has(FaultCode.PAY_104)
    assert machine.gateway.pending_payouts() == []
    await ack_policy(machine, cfg, avail)

    machine.vmc.clear_fault(FaultCode.PAY_104.value, by="admin")
    await settle(12)

    assert [c[2] for c in dispatcher.commands("cash_sale")] == ["cs1"]
