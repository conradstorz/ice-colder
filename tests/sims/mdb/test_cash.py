from ice_contracts.board.payment import CoinRoute, PayoutStatus
from ice_sims.mdb.cash import CashBox, Tube


def test_default_tubes():
    cash = CashBox()
    assert [(t.cents, t.count) for t in cash.tubes] == [
        (5, 40),
        (10, 40),
        (25, 40),
        (100, 0),
    ]
    assert cash.stacker_capacity == 500


def test_accept_coin_tubes_then_cashbox_when_full():
    cash = CashBox(tubes=[Tube(25, 1, 2)])
    assert cash.accept_coin(25) is CoinRoute.tubes
    assert cash.accept_coin(25) is CoinRoute.cashbox
    assert cash.accept_coin(100) is CoinRoute.cashbox
    assert cash.escrow_cents == 150


def test_payout_largest_first():
    cash = CashBox(escrow_cents=190)
    out = cash.payout(190)
    assert (out.status, out.paid_cents) == (PayoutStatus.ok, 190)
    levels = {t.cents: t.count for t in cash.tubes}
    assert levels == {5: 39, 10: 39, 25: 33, 100: 0}  # 7x25 + 1x10 + 1x5
    assert cash.escrow_cents == 0


def test_partial_reports_exact_paid():
    cash = CashBox(tubes=[Tube(25, 2, 10), Tube(5, 0, 10)], escrow_cents=100)
    out = cash.payout(80)
    assert out.status is PayoutStatus.partial
    assert out.paid_cents == 50
    assert cash.escrow_cents == 50


def test_failed_on_empty_tubes():
    cash = CashBox(tubes=[Tube(25, 0, 10)], escrow_cents=100)
    out = cash.payout(25)
    assert (out.status, out.paid_cents) == (PayoutStatus.failed, 0)
    assert cash.escrow_cents == 100


def test_payout_above_escrow_floors_escrow_at_zero():
    cash = CashBox(escrow_cents=25)
    out = cash.payout(100)
    assert out.paid_cents == 100
    assert cash.escrow_cents == 0


def test_cash_sale_exact_and_clamp():
    cash = CashBox(escrow_cents=300)
    assert cash.cash_sale(100) == (100, False)
    assert cash.escrow_cents == 200
    assert cash.cash_sale(500) == (200, True)
    assert cash.escrow_cents == 0


def test_stack_bill_and_models():
    cash = CashBox()
    cash.escrow_bill(500)
    assert cash.bill_in_escrow_cents == 500
    cash.stack_bill(500)
    assert cash.bill_in_escrow_cents is None
    assert cash.escrow_cents == 500
    assert cash.stacker_state().count == 1
    assert cash.tube_levels()[0].cents == 5
