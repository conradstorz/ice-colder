# tests/test_fault_registry.py
"""Unit tests for `controller.fault_registry.FaultRegistry`, in isolation
from `VMC` -- no FSM, no MQTT, no health monitor. Picks concrete codes
from `contracts.vending_machine.FAULT_TABLE` and asserts against the
specs rather than hard-coding descriptions, so a future wording change to
a description doesn't make this file lie.

Codes used:
- ICE_301: severity=lockout, scope=product -> locks.
- ICE_202: severity=vend_failed, scope=product -> does not lock, not
  recorded anywhere (the registry's own dead corner, by design).
- PAY_101: severity=product_unavailable, scope=machine -> scope wins over
  severity; recorded as a machine fault, never a lockout.
- ENV_101: severity=warning, scope=machine -> plain machine fault.
"""

from contracts.vending_machine import FAULT_TABLE, FaultCode, Severity
from controller.fault_registry import FaultRegistry, severity_level


def make_registry(names: dict[str, str] | None = None) -> FaultRegistry:
    names = names or {}

    def product_name(sku):
        if sku is None:
            return None
        return names.get(sku, sku)

    return FaultRegistry(product_name)


# --- raise_fault: product-scope lockout ---


def test_raise_lockout_code_locks_product_once():
    reg = make_registry()
    raised = reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert raised.newly_locked is True
    assert reg.lockouts == {"ICE-1": FaultCode.ICE_301}

    # Repeat raise of the same code on the same sku: already locked under
    # this code, so not "newly" locked again, and the dict is unchanged.
    raised_again = reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert raised_again.newly_locked is False
    assert reg.lockouts == {"ICE-1": FaultCode.ICE_301}


def test_raise_different_lockout_code_overwrites_and_reports_newly_locked():
    reg = make_registry()
    reg.raise_fault(FaultCode.ICE_301, sku="W-1")
    raised = reg.raise_fault(FaultCode.CFG_101, sku="W-1")
    assert raised.newly_locked is True
    assert reg.lockouts == {"W-1": FaultCode.CFG_101}


# --- raise_fault: product-scope, non-locking severity ---


def test_raise_vend_failed_severity_does_not_lock():
    assert FAULT_TABLE[FaultCode.ICE_202].severity is Severity.vend_failed
    reg = make_registry()
    raised = reg.raise_fault(FaultCode.ICE_202, sku="ICE-1")
    assert raised.newly_locked is False
    assert reg.lockouts == {}
    assert reg.machine_faults == {}


# --- raise_fault: scope decides the branch, not severity ---


def test_raise_machine_scope_product_unavailable_severity_is_machine_fault():
    spec = FAULT_TABLE[FaultCode.PAY_101]
    assert spec.severity is Severity.product_unavailable
    from contracts.vending_machine import Scope

    assert spec.scope is Scope.machine

    reg = make_registry()
    raised = reg.raise_fault(FaultCode.PAY_101)
    assert raised.newly_locked is False
    assert reg.lockouts == {}
    assert FaultCode.PAY_101 in reg.machine_faults


# --- raise_fault: plain machine fault, monotonic timestamp kept on repeat ---


def test_raise_machine_code_records_once_and_keeps_first_timestamp():
    reg = make_registry()
    reg.raise_fault(FaultCode.ENV_101)
    assert FaultCode.ENV_101 in reg.machine_faults
    first_ts = reg.machine_faults[FaultCode.ENV_101]

    reg.raise_fault(FaultCode.ENV_101)
    assert reg.machine_faults[FaultCode.ENV_101] == first_ts


# --- message / alert-key formatting ---


def test_message_formatting_with_product_name_and_outcome():
    reg = make_registry({"ICE-1": "Big Bag of Ice"})
    raised = reg.raise_fault(FaultCode.ICE_301, sku="ICE-1", outcome="timeout")
    spec = FAULT_TABLE[FaultCode.ICE_301]
    assert raised.message == (
        f"{FaultCode.ICE_301.value} {spec.description} — product 'Big Bag of Ice' "
        "(reported: timeout)"
    )
    assert raised.alert_key == "ICE-301:ICE-1"
    assert raised.level == severity_level(spec.severity)


def test_message_formatting_without_product_name_or_outcome():
    reg = make_registry()
    raised = reg.raise_fault(FaultCode.ENV_101)
    spec = FAULT_TABLE[FaultCode.ENV_101]
    assert raised.message == f"{FaultCode.ENV_101.value} {spec.description}"
    assert raised.alert_key == "ENV-101:machine"


def test_message_formatting_falls_back_to_sku_when_product_unknown():
    # product_name callable returns the sku itself when it has no better
    # name (mirrors VMC._product_name's fallback for a removed product).
    reg = make_registry()
    raised = reg.raise_fault(FaultCode.ICE_301, sku="GHOST-1")
    assert "product 'GHOST-1'" in raised.message


# --- pop_lockout / clear_machine ---


def test_pop_lockout_returns_code_and_removes_it():
    reg = make_registry()
    reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert reg.pop_lockout("ICE-1") is FaultCode.ICE_301
    assert "ICE-1" not in reg.lockouts
    assert reg.pop_lockout("ICE-1") is None


def test_clear_machine_returns_false_when_not_active():
    reg = make_registry()
    assert reg.clear_machine(FaultCode.ENV_101) is False


def test_clear_machine_returns_true_and_removes_when_active():
    reg = make_registry()
    reg.raise_fault(FaultCode.ENV_101)
    assert reg.clear_machine(FaultCode.ENV_101) is True
    assert FaultCode.ENV_101 not in reg.machine_faults
    assert reg.clear_machine(FaultCode.ENV_101) is False


# --- parse_key ---


def test_parse_key_recognizes_a_real_code():
    reg = make_registry()
    assert reg.parse_key("ENV-101") is FaultCode.ENV_101


def test_parse_key_returns_none_for_a_sku_like_string():
    reg = make_registry()
    assert reg.parse_key("ICE-1") is None


# --- has / is_locked ---


def test_has_and_is_locked_queries():
    reg = make_registry()
    assert reg.has(FaultCode.ENV_101) is False
    assert reg.is_locked("ICE-1") is None

    reg.raise_fault(FaultCode.ENV_101)
    reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert reg.has(FaultCode.ENV_101) is True
    assert reg.is_locked("ICE-1") is FaultCode.ICE_301


# --- snapshot() ordering and keys ---


def test_snapshot_orders_product_faults_before_machine_faults():
    reg = make_registry({"ICE-1": "Big Bag of Ice"})
    reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    reg.raise_fault(FaultCode.ENV_101)

    snap = reg.snapshot()
    assert [row["key"] for row in snap] == ["ICE-1", "ENV-101"]

    product_row, machine_row = snap
    ice_spec = FAULT_TABLE[FaultCode.ICE_301]
    assert product_row == {
        "key": "ICE-1",
        "sku": "ICE-1",
        "product": "Big Bag of Ice",
        "code": FaultCode.ICE_301.value,
        "severity": ice_spec.severity.value,
        "scope": ice_spec.scope.value,
        "description": ice_spec.description,
    }

    env_spec = FAULT_TABLE[FaultCode.ENV_101]
    assert machine_row == {
        "key": "ENV-101",
        "sku": None,
        "product": None,
        "code": FaultCode.ENV_101.value,
        "severity": env_spec.severity.value,
        "scope": env_spec.scope.value,
        "description": env_spec.description,
    }


def test_snapshot_preserves_insertion_order_within_each_group():
    reg = make_registry()
    reg.raise_fault(FaultCode.ICE_301, sku="ICE-1")
    reg.raise_fault(FaultCode.CFG_101, sku="W-1")
    reg.raise_fault(FaultCode.ENV_101)
    reg.raise_fault(FaultCode.WTR_103)

    snap = reg.snapshot()
    assert [row["key"] for row in snap] == ["ICE-1", "W-1", "ENV-101", "WTR-103"]
