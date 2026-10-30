"""The sale FSM pays through a `Payments` port: refunds are cash-only payouts,
a deposit carries its event id, the coin-return lever, and test sales never
touch the board."""

import asyncio

from ice_common.config_model import ConfigModel, Product
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from tests.fakes import FakePayments, FakeTaskRunner


def build(products=None):
    cfg = ConfigModel()
    cfg.physical.products = products or [
        Product(sku="ICE-1", name="Ice", price_cents=250, slot=0, kind="ice")
    ]
    payments = FakePayments()
    machine = Machine(cfg, payments=payments, tasks=FakeTaskRunner())
    machine.attach_to_loop(asyncio.get_running_loop())
    return machine, machine.vmc, payments


async def test_deposit_funds_records_the_event_id_on_the_credit():
    machine, vmc, _ = build()
    vmc.deposit_funds(100, "cash_coin", event_id="evt-1")
    assert [(c.cents, c.event_id) for c in vmc.escrow_credits] == [(100, "evt-1")]


async def test_a_failing_customer_message_cannot_undo_or_repeat_a_credit():
    machine, vmc, _ = build()

    def boom(_text):
        raise RuntimeError("display exploded")

    vmc.outputs.set_message_callback(boom)
    vmc.deposit_funds(100, "cash_coin", event_id="evt-1")  # must not raise
    assert vmc.credit_escrow_cents == 100 and len(vmc.escrow_credits) == 1


async def test_refund_pays_out_the_cash_credit_through_payments():
    machine, vmc, payments = build()
    vmc.deposit_funds(300, "cash_bill")

    vmc.request_refund(reason="cancel")

    assert payments.payouts == [(300, "cancel", None)]
    assert vmc.credit_escrow_cents == 0


async def test_refund_of_non_cash_credit_raises_pay103_and_pays_nothing():
    machine, vmc, payments = build()
    vmc.deposit_funds(300, "card")

    vmc.request_refund(reason="cancel")

    assert payments.payouts == []
    assert vmc.credit_escrow_cents == 0
    assert vmc.faults.has(FaultCode.PAY_103)


async def test_mixed_credit_pays_cash_part_and_flags_the_rest():
    machine, vmc, payments = build()
    vmc.deposit_funds(200, "cash_coin")
    vmc.deposit_funds(50, "card")

    vmc.request_refund(reason="error")

    assert payments.payouts == [(200, "error", None)]
    assert vmc.faults.has(FaultCode.PAY_103)


async def test_unknown_refund_reason_is_paid_out_as_error():
    machine, vmc, payments = build()
    vmc.deposit_funds(100, "cash_coin")
    vmc.request_refund(reason="no-such-reason")
    assert payments.payouts == [(100, "error", None)]


async def test_failed_vend_with_nothing_sellable_pays_out_as_vend_failed(caplog):
    machine, vmc, payments = build()
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(250, "cash_coin")
    vmc.process_payment()
    assert vmc.state == "dispensing"

    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "jam"})

    assert payments.payouts == [(250, "vend_failed", None)]
    assert "reason=vend_failed (ICE-" in caplog.text  # fault code in the journal


async def test_lever_refunds_the_credit_with_reason_customer():
    machine, vmc, payments = build()
    vmc.deposit_funds(100, "cash_coin")
    vmc.on_payment_lever()
    assert payments.payouts == [(100, "customer", None)]


async def test_lever_during_dispensing_is_ignored():
    machine, vmc, payments = build()
    vmc.deposit_funds(100, "cash_coin")
    vmc.set_state("dispensing")
    vmc.on_payment_lever()
    assert payments.payouts == [] and vmc.credit_escrow_cents == 100


async def test_lever_with_nothing_in_escrow_pays_nothing():
    machine, vmc, payments = build()
    vmc.on_payment_lever()
    assert payments.payouts == []


async def test_snapshot_lists_pending_payouts():
    machine, vmc, payments = build()
    vmc.deposit_funds(100, "cash_coin")
    vmc.request_refund(reason="cancel")
    (entry,) = vmc.snapshot().pending_payouts
    assert (entry["kind"], entry["cents"], entry["reason"]) == ("payout", 100, "cancel")
    assert vmc.snapshot().is_open()


async def test_a_test_sale_never_touches_payments():
    machine, vmc, payments = build()
    granted, _ = machine.lease.begin_maintenance("u", "s")
    assert granted
    assert vmc.begin_test_sale(vmc.products[0])
    vmc.process_payment()
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "jam"})
    vmc.end_test_sale()

    assert payments.payouts == [] and payments.cash_sales == []
    assert vmc.credit_escrow_cents == 0
