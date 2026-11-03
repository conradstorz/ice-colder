import pytest

from ice_contracts.board.payment import DeviceStatusEvent, PaymentState
from ice_contracts.board.vending_machine import FaultCode
from ice_edge.faults.availability import Availability, PermissiveState
from ice_edge.payment.mdb.state import StateConsumer


class FakeFaults:
    def __init__(self):
        self.active: set[FaultCode] = set()
        self.calls: list[tuple] = []

    def has(self, code):
        return code in self.active

    def raise_fault(self, code, **kw):
        self.calls.append(("raise", code))
        self.active.add(code)

    def clear_fault(self, key, by="admin"):
        self.calls.append(("clear", key, by))
        self.active.discard(FaultCode(key))
        return True


class FakePolicy:
    def __init__(self):
        self.states = []

    def on_state(self, state):
        self.states.append(state)


def doc(
    coin="ready",
    bill="ready",
    reason=None,
    exact=False,
    coin_present=True,
    bill_present=True,
):
    return {
        "boot_id": "boot-0001",
        "seq_last": 0,
        "policy_id": "p1",
        "accepting": reason is None,
        "inhibited_reason": reason,
        "escrow_cents": 0,
        "bill_in_escrow_cents": None,
        "devices": {
            "coin": {"present": coin_present, "status": coin},
            "bill": {"present": bill_present, "status": bill},
            "cashless": {"present": False, "status": "offline"},
        },
        "tubes": [],
        "exact_change": exact,
        "stacker": None,
        "session": None,
    }


@pytest.fixture
def rig():
    faults, policy, avail, msgs = FakeFaults(), FakePolicy(), Availability(), []
    c = StateConsumer(
        faults=faults, availability=lambda: avail, policy=policy, message=msgs.append
    )
    return c, faults, policy, avail, msgs


def rows(avail):
    return {r["name"]: r for r in avail.table()}


def st(avail, name):
    return rows(avail)[name]["state"]


async def test_no_state_yet_rows_unknown(rig):
    _, _, _, avail, _ = rig
    for n in ("payment_coin_ready", "payment_bill_ready", "cash_accepting"):
        assert st(avail, n) == PermissiveState.UNKNOWN.value
        assert rows(avail)[n]["detail"] == "no report yet"


@pytest.mark.parametrize("status", ["offline", "error", "tilt", "jammed"])
async def test_bad_status_fails_device_row_and_raises_fault(rig, status):
    c, faults, _, avail, _ = rig
    await c.handle("payment/state", doc(coin=status))
    assert st(avail, "payment_coin_ready") == "fail"
    assert st(avail, "payment_bill_ready") == "pass"
    assert FaultCode.PAY_106 in faults.active
    assert FaultCode.PAY_107 not in faults.active


@pytest.mark.parametrize("status", ["ready", "disabled", "busy", "full", "empty"])
async def test_ok_statuses_pass_device_row(rig, status):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(bill=status))
    assert st(avail, "payment_bill_ready") == "pass"


async def test_full_empty_alert_only_and_leave_fault_untouched(rig):
    c, faults, _, avail, _ = rig
    await c.handle("payment/state", doc(coin="jammed"))
    await c.handle("payment/state", doc(coin="full"))
    assert rows(avail)["payment_coin_ready"]["detail"] == "full"
    assert FaultCode.PAY_106 in faults.active


async def test_gates(rig):
    _, _, _, avail, _ = rig
    r = rows(avail)
    assert r["payment_coin_ready"]["gate"] == "alert"
    assert r["payment_bill_ready"]["gate"] == "alert"
    assert r["cash_accepting"]["gate"] == "fulfillment"
    assert "payment_devices_ready" not in r


async def test_one_jammed_device_keeps_cash_accepting(rig):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(coin="jammed"))
    assert st(avail, "cash_accepting") == "pass"


async def test_both_unusable_fails_cash_accepting(rig):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(coin="jammed", bill="offline"))
    assert st(avail, "cash_accepting") == "fail"


async def test_no_present_cash_device_fails(rig):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(coin_present=False, bill_present=False))
    assert st(avail, "cash_accepting") == "fail"


@pytest.mark.parametrize("reason", ["edge_silent", "device_fault"])
async def test_inhibit_reasons_fail_cash_accepting(rig, reason):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(reason=reason))
    assert st(avail, "cash_accepting") == "fail"


@pytest.mark.parametrize("reason", ["policy", "no_policy"])
async def test_policy_reasons_do_not_fail_cash_accepting(rig, reason):
    c, _, _, avail, _ = rig
    await c.handle("payment/state", doc(reason=reason))
    assert st(avail, "cash_accepting") == "pass"


async def test_cash_accepting_blocks_sale_not_payment(rig):
    c, _, _, avail, _ = rig
    avail.set_payment_state(None)
    await c.handle("payment/state", doc(coin="error", bill="error"))
    assert "cash_accepting" in avail.sale_available("ice")[1]
    assert avail.payment_enabled is True


async def test_faults_clear_from_state_and_skip_inactive(rig):
    c, faults, _, _, _ = rig
    await c.handle("payment/state", doc(bill="tilt"))
    await c.handle("payment/state", doc(bill="tilt"))
    assert faults.calls == [("raise", FaultCode.PAY_107)]
    await c.handle("payment/state", doc(bill="ready"))
    await c.handle("payment/state", doc(bill="ready"))
    assert faults.calls[1:] == [("clear", "PAY-107", "auto")]


async def test_device_status_event_raises_and_clears(rig):
    c, faults, _, _, _ = rig
    c.on_device_status(
        DeviceStatusEvent(kind="device_status", device="coin", status="jammed")
    )
    assert FaultCode.PAY_106 in faults.active
    c.on_device_status(
        DeviceStatusEvent(kind="device_status", device="coin", status="busy")
    )
    assert FaultCode.PAY_106 not in faults.active
    c.on_device_status(
        DeviceStatusEvent(kind="device_status", device="cashless", status="error")
    )
    assert faults.calls == [("raise", FaultCode.PAY_106), ("clear", "PAY-106", "auto")]


async def test_exact_change_message_once_per_flip(rig):
    c, _, _, _, msgs = rig
    await c.handle("payment/state", doc(exact=False))
    await c.handle("payment/state", doc(exact=True))
    await c.handle("payment/state", doc(exact=True))
    assert msgs == ["Exact change only"]
    await c.handle("payment/state", doc(exact=False))
    await c.handle("payment/state", doc(exact=True))
    assert msgs == ["Exact change only"] * 2


async def test_policy_and_subscribers_called_and_state_stored(rig):
    c, _, policy, _, _ = rig
    seen = []
    c.subscribe_state(seen.append)
    await c.handle("payment/state", doc())
    assert len(policy.states) == 1 and isinstance(policy.states[0], PaymentState)
    assert seen == policy.states
    assert c.state is policy.states[0] and c.state_ts > 0


async def test_invalid_document_dropped(rig):
    c, faults, policy, avail, msgs = rig
    await c.handle("payment/state", {"boot_id": "x"})
    assert c.state is None and policy.states == [] and not faults.calls
    assert st(avail, "cash_accepting") == "unknown"


async def test_absent_device_clears_active_fault(rig):
    c, faults, _, _, _ = rig
    await c.handle("payment/state", doc(coin="jammed"))
    assert FaultCode.PAY_106 in faults.active
    await c.handle("payment/state", doc(coin="offline", coin_present=False))
    assert FaultCode.PAY_106 not in faults.active
    assert faults.calls[-1] == ("clear", "PAY-106", "auto")


async def test_exact_change_on_first_state_fires(rig):
    c, _, _, _, msgs = rig
    await c.handle("payment/state", doc(exact=True))
    assert msgs == ["Exact change only"]
