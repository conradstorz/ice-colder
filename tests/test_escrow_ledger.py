"""Unit tests for controller.escrow_ledger.EscrowLedger -- ledger only, no VMC."""

from controller.escrow_ledger import EscrowLedger
from services.session_store import Credit


def test_deposit_updates_total_and_credits():
    ledger = EscrowLedger()

    ledger.deposit("cash_bill", 2.50, ts=100.0)

    assert ledger.total == 2.50
    assert ledger.credits == [Credit(method="cash_bill", amount=2.50, ts=100.0)]


def test_deposit_accumulates_across_multiple_calls():
    ledger = EscrowLedger()

    ledger.deposit("cash_bill", 1.00, ts=1.0)
    ledger.deposit("card", 0.50, ts=2.0)

    assert ledger.total == 1.50
    assert len(ledger.credits) == 2


def test_consume_fifo_splits_middle_credit_across_two_methods():
    """Three credits, cash/card/cash, where price lands in the middle of
    the second credit -- the first is fully consumed, the second shrinks
    in place (keeping its method and ts), the third is untouched."""
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 1.00, ts=1.0)
    ledger.deposit("card", 1.00, ts=2.0)
    ledger.deposit("cash_bill", 1.00, ts=3.0)

    shares = ledger.consume_fifo(1.50)

    assert shares == {"cash_bill": 1.00, "card": 0.50}
    assert ledger.credits == [
        Credit(method="card", amount=0.50, ts=2.0),
        Credit(method="cash_bill", amount=1.00, ts=3.0),
    ]
    # consume_fifo never touches total -- the caller subtracts separately.
    assert ledger.total == 3.00


def test_consume_fifo_divergence_guard_books_unknown_and_leaves_credits():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 1.00, ts=1.0)
    ledger.total = 1.01  # nudge out of sync without touching credits

    shares = ledger.consume_fifo(1.00)

    assert shares == {"unknown": 1.00}
    assert ledger.credits == [Credit(method="cash_bill", amount=1.00, ts=1.0)]


def test_restore_appends_one_credit_per_share_in_order_and_adds_to_total():
    ledger = EscrowLedger()
    ledger.total = 0.0

    ledger.restore({"cash_bill": 2.00, "card": 0.50}, ts=5.0, price=2.50)

    assert ledger.total == 2.50
    assert ledger.credits == [
        Credit(method="cash_bill", amount=2.00, ts=5.0),
        Credit(method="card", amount=0.50, ts=5.0),
    ]


def test_restore_skips_zero_amount_shares_but_still_adds_price():
    ledger = EscrowLedger()

    ledger.restore({"unknown": 0.0, "cash_bill": 1.00}, ts=5.0, price=1.00)

    assert ledger.total == 1.00
    assert ledger.credits == [Credit(method="cash_bill", amount=1.00, ts=5.0)]


def test_take_all_returns_rounded_total_and_empties_both():
    """0.1 + 0.2 leaves total at the familiar 0.30000000000000004 float
    residue; take_all must round that away to exactly 0.3, not propagate it."""
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 0.1, ts=1.0)
    ledger.deposit("cash_bill", 0.2, ts=2.0)

    amount = ledger.take_all()

    assert amount == 0.3
    assert ledger.total == 0.0
    assert ledger.credits == []


def test_has_credit_false_at_zero_true_after_small_deposit():
    ledger = EscrowLedger()

    assert ledger.has_credit is False

    ledger.deposit("cash_coin", 0.01, ts=1.0)

    assert ledger.has_credit is True


def test_rounding_no_residue_after_depositing_point1_and_point2():
    """0.1 + 0.2 deposited as two credits, then 0.3 consumed, leaves no
    credits and no sub-cent residue -- consume_fifo rounds at every step."""
    ledger = EscrowLedger()
    ledger.deposit("cash_coin", 0.1, ts=1.0)
    ledger.deposit("cash_coin", 0.2, ts=2.0)

    shares = ledger.consume_fifo(0.3)

    assert shares == {"cash_coin": 0.3}
    assert ledger.credits == []


def test_snapshot_credits_returns_a_copy():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 1.00, ts=1.0)

    snap = ledger.snapshot_credits()
    snap.append(Credit(method="card", amount=5.0, ts=2.0))

    assert len(ledger.credits) == 1


def test_is_empty_within_tolerance():
    ledger = EscrowLedger()
    assert ledger.is_empty_within_tolerance is True

    ledger.total = 0.004
    assert ledger.is_empty_within_tolerance is True

    ledger.total = 0.01
    assert ledger.is_empty_within_tolerance is False
