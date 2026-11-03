from ice_contracts.board.payment import InhibitReason, SetPolicyParams
from ice_sims.mdb.cash import CashBox, Tube
from ice_sims.mdb.policy import PolicyState


def params(**kw) -> SetPolicyParams:
    base = dict(
        policy_id="p1",
        accept=True,
        max_escrow_cents=2000,
        coin_enabled=[5, 10, 25, 100],
        bill_enabled=[100, 500, 1000],
        cashless_enabled=False,
        change_threshold_cents=500,
        edge_timeout_s=30,
        return_bill_on_lever=True,
    )
    base.update(kw)
    return SetPolicyParams(**base)


def test_no_policy_before_apply():
    p = PolicyState()
    assert p.inhibited_reason() is InhibitReason.no_policy
    assert not p.accepts_coin(25, CashBox())
    assert not p.accepts_bill(100, CashBox())
    p.apply(params())
    assert p.acked_in_boot
    assert p.inhibited_reason() is None
    assert p.accepts_coin(25, CashBox())


def test_inhibit_order():
    p = PolicyState()
    p.apply(params(accept=False))
    assert p.inhibited_reason() is InhibitReason.policy
    assert p.inhibited_reason(edge_silent=True) is InhibitReason.edge_silent
    p.apply(params())
    assert p.inhibited_reason(device_fault=True) is InhibitReason.device_fault


def test_escrow_cap_refuses_bill_but_not_coin():
    p = PolicyState()
    p.apply(params(max_escrow_cents=1000))
    cash = CashBox(escrow_cents=600)
    assert not p.accepts_bill(500, cash)
    assert p.accepts_bill(100, cash)
    assert p.accepts_coin(100, cash)


def test_exact_change_flips_at_threshold():
    p = PolicyState()
    p.apply(params(change_threshold_cents=500))
    assert p.exact_change(CashBox(tubes=[Tube(25, 19, 50)])) is True  # 475
    assert p.exact_change(CashBox(tubes=[Tube(25, 20, 50)])) is False  # 500


def test_exact_change_refuses_big_bill():
    p = PolicyState()
    p.apply(params(change_threshold_cents=500))
    low = CashBox(tubes=[Tube(25, 10, 50)])
    assert not p.accepts_bill(1000, low)
    assert p.accepts_bill(500, low)  # not above threshold
    assert p.accepts_bill(1000, CashBox())  # tubes healthy


def test_reset_forgets_policy():
    p = PolicyState()
    p.apply(params())
    p.reset()
    assert p.inhibited_reason() is InhibitReason.no_policy
