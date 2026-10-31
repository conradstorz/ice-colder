"""Unit tests for ice_edge.ledger.escrow_ledger.EscrowLedger -- ledger only, no VMC.

All amounts are integer cents.
"""

from ice_edge.ledger.escrow_ledger import EscrowLedger
from ice_edge.ledger.session_store import Credit


def test_deposit_updates_total_and_credits():
    ledger = EscrowLedger()

    ledger.deposit("cash_bill", 250, ts=100.0)

    assert ledger.total_cents == 250
    assert ledger.credits == [Credit(method="cash_bill", cents=250, ts=100.0)]


def test_deposit_accumulates_across_multiple_calls():
    ledger = EscrowLedger()

    ledger.deposit("cash_bill", 100, ts=1.0)
    ledger.deposit("card", 50, ts=2.0)

    assert ledger.total_cents == 150
    assert len(ledger.credits) == 2


def test_consume_fifo_splits_middle_credit_across_two_methods():
    """Three credits, cash/card/cash, where price lands in the middle of
    the second credit -- the first is fully consumed, the second shrinks
    in place (keeping its method and ts), the third is untouched."""
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 100, ts=1.0)
    ledger.deposit("card", 100, ts=2.0)
    ledger.deposit("cash_bill", 100, ts=3.0)

    shares = ledger.consume_fifo(150)

    assert shares == {"cash_bill": 100, "card": 50}
    assert ledger.credits == [
        Credit(method="card", cents=50, ts=2.0),
        Credit(method="cash_bill", cents=100, ts=3.0),
    ]
    # consume_fifo never touches the total -- the caller subtracts separately.
    assert ledger.total_cents == 300


def test_consume_fifo_divergence_guard_fires_on_a_one_cent_mismatch():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 100, ts=1.0)
    ledger.total_cents = 101  # nudge out of sync without touching credits

    shares = ledger.consume_fifo(100)

    assert shares == {"unknown": 100}
    assert ledger.credits == [Credit(method="cash_bill", cents=100, ts=1.0)]


def test_restore_appends_one_credit_per_share_in_order_and_adds_to_total():
    ledger = EscrowLedger()

    ledger.restore({"cash_bill": 200, "card": 50}, ts=5.0, price_cents=250)

    assert ledger.total_cents == 250
    assert ledger.credits == [
        Credit(method="cash_bill", cents=200, ts=5.0),
        Credit(method="card", cents=50, ts=5.0),
    ]


def test_restore_skips_zero_cent_shares_but_still_adds_price():
    ledger = EscrowLedger()

    ledger.restore({"unknown": 0, "cash_bill": 100}, ts=5.0, price_cents=100)

    assert ledger.total_cents == 100
    assert ledger.credits == [Credit(method="cash_bill", cents=100, ts=5.0)]


def test_restore_shares_returns_the_int_restored():
    ledger = EscrowLedger()

    restored = ledger.restore_shares({"cash_bill": 100, "card": 25})

    assert restored == 125
    assert ledger.total_cents == 125


def test_take_all_returns_the_int_total_and_empties_both():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 10, ts=1.0)
    ledger.deposit("cash_bill", 20, ts=2.0)

    cents = ledger.take_all()

    assert cents == 30
    assert isinstance(cents, int)
    assert ledger.total_cents == 0
    assert ledger.credits == []


def test_has_credit_false_at_zero_true_after_one_cent():
    ledger = EscrowLedger()

    assert ledger.has_credit is False

    ledger.deposit("cash_coin", 1, ts=1.0)

    assert ledger.has_credit is True


def test_exact_arithmetic_leaves_no_residue_after_ten_plus_twenty_cents():
    """10c + 20c consumed as 30c leaves no credits and an exact total."""
    ledger = EscrowLedger()
    ledger.deposit("cash_coin", 10, ts=1.0)
    ledger.deposit("cash_coin", 20, ts=2.0)

    shares = ledger.consume_fifo(30)

    assert shares == {"cash_coin": 30}
    assert ledger.credits == []


def test_snapshot_credits_returns_a_copy():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 100, ts=1.0)

    snap = ledger.snapshot_credits()
    snap.append(Credit(method="card", cents=500, ts=2.0))

    assert len(ledger.credits) == 1


def test_is_empty_is_exact():
    ledger = EscrowLedger()
    assert ledger.is_empty is True

    ledger.total_cents = 1
    assert ledger.is_empty is False


def test_event_id_travels_with_a_partially_consumed_credit():
    ledger = EscrowLedger()
    ledger.deposit("cash_bill", 500, 1.0, event_id="evt-boot-0001-0001")
    ledger.consume_fifo(200)
    assert [(c.cents, c.event_id) for c in ledger.credits] == [
        (300, "evt-boot-0001-0001")
    ]


def test_consume_fifo_pieces_reports_the_consumed_event_ids():
    ledger = EscrowLedger()
    ledger.deposit("cash_coin", 100, 1.0, "b1-1")
    ledger.deposit("cash_coin", 100, 2.0, "b2-1")
    ledger.deposit("card", 100, 3.0)
    shares, pieces = ledger.consume_fifo_pieces(150)
    assert shares == {"cash_coin": 150}
    assert [(c.method, c.cents, c.event_id) for c in pieces] == [
        ("cash_coin", 100, "b1-1"),
        ("cash_coin", 50, "b2-1"),
    ]
    # the partly consumed credit keeps its event id
    assert [(c.cents, c.event_id) for c in ledger.credits] == [
        (50, "b2-1"),
        (100, None),
    ]


def test_consume_fifo_still_returns_just_the_shares():
    ledger = EscrowLedger()
    ledger.deposit("cash_coin", 100, 1.0, "b1-1")
    assert ledger.consume_fifo(100) == {"cash_coin": 100}


def test_restore_with_pieces_keeps_each_event_id():
    ledger = EscrowLedger()
    ledger.deposit("cash_coin", 100, 1.0, "b1-1")
    ledger.deposit("cash_coin", 100, 2.0, "b2-1")
    shares, pieces = ledger.consume_fifo_pieces(200)
    ledger.total_cents -= 200
    ledger.restore(shares, 9.0, 200, pieces)
    assert ledger.total_cents == 200
    assert [(c.cents, c.event_id) for c in ledger.credits] == [
        (100, "b1-1"),
        (100, "b2-1"),
    ]


def test_restore_ignores_pieces_that_do_not_sum_to_the_shares():
    ledger = EscrowLedger()
    ledger.restore({"cash_coin": 100}, 9.0, 100, [Credit("cash_coin", 40, 1.0, "x-1")])
    assert [(c.cents, c.event_id) for c in ledger.credits] == [(100, None)]
