from ice_contracts.board.payment import PaymentState, PayoutReason
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.payment.mdb.reconcile import Reconciler
from tests.fakes import FakeFaults

BOOT = "boot-0001"


def mk_state(escrow=100, seq_last=0, boot=BOOT):
    return PaymentState(
        boot_id=boot,
        seq_last=seq_last,
        policy_id="p1",
        accepting=True,
        inhibited_reason=None,
        escrow_cents=escrow,
        bill_in_escrow_cents=None,
        devices={
            "coin": {"present": True, "status": "ready"},
            "bill": {"present": True, "status": "ready"},
            "cashless": {"present": False, "status": "offline"},
        },
        tubes=[],
        exact_change=False,
        stacker=None,
        session=None,
    )


class FakeIntake:
    def __init__(self):
        self.seqs = {}
        self.last_processed_ts = None

    def caught_up_to(self, boot):
        return self.seqs.get(boot, 0)


class FakePayouts:
    def __init__(self):
        self.idle = True
        self.calls = []

    def pay_out(self, cents, reason, sale_id):
        self.calls.append((cents, reason, sale_id))


class Env:
    def __init__(self, board=100, edge=100):
        self.state = mk_state(board)
        self.state_ts = 10.0
        self.edge = edge
        self.edge_by_boot = lambda boot: self.edge
        self.idle = True
        self.pay104 = False
        self.intake = FakeIntake()
        self.payouts = FakePayouts()
        self.faults = FakeFaults()
        self.rec = Reconciler(
            state=lambda: self.state,
            state_ts=lambda: self.state_ts,
            intake=self.intake,
            payouts=self.payouts,
            fsm_idle=lambda: self.idle,
            pay104_active=lambda: self.pay104,
            edge_cash_escrow=lambda boot: self.edge_by_boot(boot),
            faults=self.faults,
        )
        self.rec.arm("t")


def test_equal_does_nothing():
    e = Env(board=100, edge=100)
    e.rec.maybe_run()
    assert e.payouts.calls == [] and e.faults.raised == []


def test_board_higher_pays_out_difference():
    e = Env(board=175, edge=100)
    e.rec.maybe_run()
    assert e.payouts.calls == [(75, PayoutReason.error, None)]


def test_board_lower_raises_pay104():
    e = Env(board=25, edge=100)
    e.rec.maybe_run()
    assert e.faults.raised == [(FaultCode.PAY_104, "escrow_mismatch:100/25")]
    assert e.payouts.calls == []


def test_runs_once_per_arm():
    e = Env(board=150, edge=100)
    e.rec.maybe_run()
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1
    e.rec.arm("again")
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 2  # partial-shortfall retry on next trigger


def test_not_idle_blocks_then_runs_when_idle():
    e = Env(board=150, edge=100)
    e.idle = False
    e.rec.maybe_run()
    assert e.payouts.calls == []
    e.idle = True
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_pay104_blocks_and_stays_armed_until_cleared():
    # Crash-after-dispense: uncertain snapshot (PAY-104) while the board escrow
    # still holds the price. No payout may be made.
    e = Env(board=150, edge=0)
    e.pay104 = True
    e.rec.maybe_run()
    e.rec.maybe_run()
    assert e.payouts.calls == [] and e.faults.raised == []
    e.pay104 = False
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_intake_not_caught_up_blocks():
    e = Env(board=150, edge=100)
    e.state = mk_state(150, seq_last=3)
    e.intake.seqs[BOOT] = 2
    e.rec.maybe_run()
    assert e.payouts.calls == []
    e.intake.seqs[BOOT] = 3
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_queue_not_empty_blocks():
    e = Env(board=150, edge=100)
    e.payouts.idle = False
    e.rec.maybe_run()
    assert e.payouts.calls == []
    e.payouts.idle = True
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_state_older_than_last_event_blocks():
    e = Env(board=150, edge=100)
    e.intake.last_processed_ts = 20.0
    e.rec.maybe_run()
    assert e.payouts.calls == []
    e.state_ts = 21.0
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_no_state_yet_waits():
    e = Env()
    e.state = None
    e.rec.maybe_run()
    e.state = mk_state(150)
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1


def test_unarmed_does_nothing():
    e = Env(board=100, edge=100)
    e.rec.maybe_run()  # consumes the arm
    e.state = mk_state(150)
    e.rec.maybe_run()
    assert e.payouts.calls == []


def test_new_boot_id_arms():
    e = Env(board=150, edge=100)
    e.rec.on_state(e.state)
    assert len(e.payouts.calls) == 1
    e.rec.on_state(mk_state(150, boot="boot-0002"))
    assert len(e.payouts.calls) == 2


def test_edge_cash_is_asked_for_the_boards_current_boot():
    asked = []
    e = Env(board=100, edge=100)
    e.edge_by_boot = lambda boot: asked.append(boot) or 100
    e.rec.maybe_run()
    assert asked == [BOOT]


def test_earlier_boot_cash_is_not_counted_against_the_new_boot():
    # The board rebooted: its escrow is 0 and only current-boot cash counts, so
    # the edge's earlier-boot cash causes neither PAY-104 nor a payout.
    e = Env(board=0, edge=0)
    e.edge_by_boot = lambda boot: {"boot-0002": 0, BOOT: 300}.get(boot, 0)
    e.state = mk_state(0, boot="boot-0002")
    e.rec.maybe_run()
    assert e.faults.raised == [] and e.payouts.calls == []


def test_mixed_boot_credits_reconcile_on_the_current_boot_part_only():
    e = Env(board=150, edge=0)
    e.edge_by_boot = lambda boot: {BOOT: 100}.get(boot, 0)
    e.rec.maybe_run()
    assert e.payouts.calls == [(50, PayoutReason.error, None)]


def test_reconnect_arm_ignores_state_cached_before_the_disconnect():
    # The state cached before the link dropped may not match the board any
    # more; it must not consume the one-shot arm. The refreshed state does.
    e = Env(board=100, edge=100)
    e.rec.maybe_run()  # consume the initial arm
    e.rec.arm("mqtt_reconnect", require_fresh_state=True)
    e.rec.maybe_run()  # stale cached state: equal, would silently disarm
    e.state, e.state_ts = mk_state(175), 20.0  # fresh state reveals excess
    e.rec.on_state(e.state)
    assert e.payouts.calls == [(75, PayoutReason.error, None)]


def test_reconnect_arm_with_no_fresh_state_stays_armed():
    e = Env(board=150, edge=100)
    e.rec.maybe_run()
    e.rec.arm("mqtt_reconnect", require_fresh_state=True)
    e.rec.maybe_run()
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 1  # only the initial arm's payout
    e.state_ts = 20.0
    e.rec.maybe_run()
    assert len(e.payouts.calls) == 2


def test_later_arm_without_fresh_requirement_keeps_the_pending_one():
    # A pay101_cleared / new_boot arm landing while the reconnect arm still
    # waits for a post-reconnect state must not drop the freshness requirement.
    e = Env(board=100, edge=100)
    e.rec.maybe_run()  # consume the initial arm
    e.state, e.state_ts = mk_state(175), 10.0  # cached pre-disconnect state
    e.rec.arm("mqtt_reconnect", require_fresh_state=True)
    e.rec.arm("pay101_cleared")
    e.rec.maybe_run()  # stale cached state: must not reconcile
    assert e.payouts.calls == [] and e.faults.raised == []
    e.state_ts = 20.0  # fresh state arrives
    e.rec.on_state(e.state)
    assert e.payouts.calls == [(75, PayoutReason.error, None)]
