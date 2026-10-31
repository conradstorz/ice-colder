"""The sale FSM pays through a `Payments` port: refunds are cash-only payouts,
a deposit carries its event id, the coin-return lever, and test sales never
touch the board."""

import asyncio

from ice_common.config_model import ConfigModel, Product
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.app.machine import Machine
from tests.dispenser_fixtures import FakeDispatcher, profiles_for
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


def build_vendable(tmp_path, products=None):
    """`build()` plus a loaded dispenser profile and a dispatcher, so a
    sale can actually run to a `complete` report."""
    machine, vmc, payments = build(products)
    machine.set_dispenser_profiles(
        profiles_for(machine.vmc.config_model.physical.products, tmp_path)
    )
    machine.set_command_dispatcher(FakeDispatcher())
    return machine, vmc, payments


async def vend(vmc, *deposits):
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    for cents, method in deposits:
        vmc.deposit_funds(cents, method)
    vmc.process_payment()
    assert vmc.state == "dispensing"
    sale_id = vmc.sale.sale_id
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "complete"})
    return sale_id


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
    sale_id = vmc.sale.sale_id

    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "jam"})

    assert payments.payouts == [(250, "vend_failed", sale_id)]
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


async def test_change_is_paid_at_once_after_an_overpaid_vend(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)

    sale_id = await vend(vmc, (500, "cash_coin"))

    assert vmc.state == "idle"
    assert vmc.credit_escrow_cents == 0
    assert payments.payouts == [(250, "change", sale_id)]
    assert payments.cash_sales == [(250, sale_id)]
    # The sale's cash_sale precedes the change payout in the board queue.
    assert [p.kind for p in payments.pending_payouts()] == ["cash_sale", "payout"]


async def test_sale_id_is_a_uuid4_hex_minted_with_the_context(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    vmc.selected_product = vmc.products[0]
    first = vmc.sale.sale_id
    assert len(first) == 32 and int(first, 16) >= 0
    vmc.selected_product = None
    vmc.selected_product = vmc.products[0]
    assert vmc.sale.sale_id != first


async def test_exact_payment_sends_cash_sale_and_no_change(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)

    sale_id = await vend(vmc, (250, "cash_bill"))

    assert vmc.state == "idle"
    assert payments.payouts == []
    assert payments.cash_sales == [(250, sale_id)]


async def test_cash_sale_carries_only_the_cash_share(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)

    sale_id = await vend(vmc, (100, "card"), (200, "cash_coin"))

    # FIFO: the 100 card cents and 150 cash cents paid the 250 price;
    # 50 cash cents are left and come back as change.
    assert payments.cash_sales == [(150, sale_id)]
    assert payments.payouts == [(50, "change", sale_id)]


async def test_all_card_sale_sends_no_cash_sale(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    await vend(vmc, (250, "card"))
    assert payments.cash_sales == []


async def test_complete_transaction_always_ends_in_idle_with_credit_left(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    await vend(vmc, (900, "cash_coin"))
    assert vmc.state == "idle"
    assert "session_timeout" not in {c.label for c in machine.tasks.scheduled}
    assert payments.payouts[0][:2] == (650, "change")


async def test_a_test_sale_that_succeeds_never_touches_payments(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    granted, _ = machine.lease.begin_maintenance("u", "s")
    assert granted
    assert vmc.begin_test_sale(vmc.products[0])
    vmc.process_payment()
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "complete"})
    vmc.end_test_sale()
    assert payments.payouts == [] and payments.cash_sales == []


async def test_lever_during_a_real_dispense_is_ignored_and_credit_is_kept(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(500, "cash_coin")
    vmc.process_payment()
    assert vmc.state == "dispensing"
    vmc.on_payment_lever()
    assert payments.payouts == [] and vmc.credit_escrow_cents == 250


async def test_payout_enqueue_failure_restores_escrow_and_raises_pay103():
    machine, vmc, payments = build()
    vmc.deposit_funds(300, "cash_coin", event_id="evt-1")

    def boom(*_a):
        raise RuntimeError("queue write failed")

    payments.pay_out = boom
    vmc.request_refund(reason="cancel")

    assert vmc.credit_escrow_cents == 300
    assert [(c.cents, c.event_id) for c in vmc.escrow_credits] == [(300, "evt-1")]
    assert vmc.faults.has(FaultCode.PAY_103)


# --- the board's escrow is per boot (firmware spec: RAM only) ----------------------


async def vend_ev(vmc, *deposits):
    """`vend` for deposits carrying board event ids: (cents, method, event_id)."""
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    for cents, method, event_id in deposits:
        vmc.deposit_funds(cents, method, event_id=event_id)
    vmc.process_payment()
    assert vmc.state == "dispensing"
    sale_id = vmc.sale.sale_id
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "complete"})
    return sale_id


async def test_cash_sale_counts_only_the_current_boot_cash(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    payments.boot_id = "boot-B"
    # 100 arrived before the board rebooted, 200 after: the price is 250.
    sale_id = await vend_ev(
        vmc, (100, "cash_coin", "boot-A-1"), (200, "cash_coin", "boot-B-1")
    )
    assert payments.cash_sales == [(150, sale_id)]
    assert payments.payouts == [(50, "change", sale_id)]


async def test_cash_sale_from_earlier_boot_cash_only_sends_none(tmp_path):
    machine, vmc, payments = build_vendable(tmp_path)
    payments.boot_id = "boot-B"
    await vend_ev(vmc, (300, "cash_bill", "boot-A-1"))
    assert payments.cash_sales == []
    assert [p[0] for p in payments.payouts] == [50]  # change comes from the tubes


async def test_failed_vend_restore_keeps_event_ids_so_the_cash_still_counts(tmp_path):
    machine, vmc, payments = build_vendable(
        tmp_path,
        [
            Product(sku="ICE-1", name="Ice", price_cents=250, slot=0, kind="ice"),
            Product(sku="ICE-2", name="Ice2", price_cents=250, slot=1, kind="ice"),
        ],
    )
    payments.boot_id = "boot-B"
    vmc.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.deposit_funds(250, "cash_coin", event_id="boot-B-1")
    vmc.process_payment()
    await vmc.on_dispenser_event("hardware/dispenser", {"slot": 0, "state": "jam"})
    assert [(c.cents, c.event_id) for c in vmc.escrow_credits] == [(250, "boot-B-1")]
