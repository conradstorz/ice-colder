"""Contract models for the vending-machine interface (v0.1.0)."""

import pytest
from pydantic import ValidationError

from contracts.vending_machine import (
    CONTRACT_VERSION,
    FAULT_TABLE,
    OUTCOME_FAULTS,
    DispenserOutcome,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
    Scope,
    Severity,
)


def test_contract_version():
    assert CONTRACT_VERSION == "0.1.0"


def test_every_fault_code_has_a_table_entry():
    missing = [c for c in FaultCode if c not in FAULT_TABLE]
    assert missing == []


def test_every_failure_outcome_maps_to_a_fault_code():
    for outcome in DispenserOutcome:
        if outcome is DispenserOutcome.complete:
            assert outcome not in OUTCOME_FAULTS
        else:
            assert isinstance(OUTCOME_FAULTS[outcome], FaultCode)


def test_outcome_mapping_matches_spec():
    assert OUTCOME_FAULTS[DispenserOutcome.bin_empty] is FaultCode.ICE_101
    assert OUTCOME_FAULTS[DispenserOutcome.timeout] is FaultCode.ICE_301
    assert OUTCOME_FAULTS[DispenserOutcome.jam] is FaultCode.ICE_401
    assert OUTCOME_FAULTS[DispenserOutcome.error] is FaultCode.ICE_302


def test_fault_code_values_are_stable_strings():
    assert FaultCode.ICE_101.value == "ICE-101"
    assert FaultCode.PAY_103.value == "PAY-103"
    assert FaultCode("ICE-301") is FaultCode.ICE_301


def test_severities_follow_roadmap():
    assert FAULT_TABLE[FaultCode.ICE_101].severity is Severity.product_unavailable
    assert FAULT_TABLE[FaultCode.ICE_301].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.ICE_401].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.ICE_302].severity is Severity.lockout
    assert FAULT_TABLE[FaultCode.PAY_102].severity is Severity.vend_failed
    assert FAULT_TABLE[FaultCode.PAY_103].severity is Severity.warning
    assert FAULT_TABLE[FaultCode.PAY_103].scope is Scope.machine
    assert FAULT_TABLE[FaultCode.ICE_301].scope is Scope.product


class TestPaymentRefundCommand:
    def test_valid(self):
        cmd = PaymentRefundCommand(
            request_id="a" * 32, amount=2.5, reason="session_timeout"
        )
        assert cmd.amount == 2.5
        assert cmd.timestamp is not None

    def test_rejects_non_positive_amount(self):
        with pytest.raises(ValidationError):
            PaymentRefundCommand(request_id="a" * 32, amount=0, reason="cancel")

    def test_rejects_short_request_id(self):
        with pytest.raises(ValidationError):
            PaymentRefundCommand(request_id="short", amount=1.0, reason="cancel")


class TestPaymentRefundResult:
    def test_defaults(self):
        res = PaymentRefundResult(request_id="a" * 32, status=RefundStatus.ok)
        assert res.amount_returned == 0.0
        assert res.detail is None

    def test_round_trip_json(self):
        res = PaymentRefundResult(
            request_id="a" * 32,
            status="failed",
            amount_returned=0.0,
            detail="changer_empty",
        )
        again = PaymentRefundResult.model_validate_json(res.model_dump_json())
        assert again.status is RefundStatus.failed
        assert again.detail == "changer_empty"


def test_vmc_alert_carries_code_and_sku():
    from services.mqtt_messages import VMCAlert

    alert = VMCAlert(
        level="error", message="x", code=FaultCode.ICE_301, product_sku="ICE-1"
    )
    data = alert.model_dump(mode="json")
    assert data["code"] == "ICE-301"
    assert data["product_sku"] == "ICE-1"
    assert VMCAlert(level="info", message="y").code is None
