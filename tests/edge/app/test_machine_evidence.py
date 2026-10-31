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
