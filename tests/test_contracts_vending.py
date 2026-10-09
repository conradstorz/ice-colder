"""Contract models for the vending-machine interface (v0.1.0)."""

import pytest
from pydantic import ValidationError

from contracts.vending_machine import (
    CONTRACT_VERSION,
    FAULT_TABLE,
    OUTCOME_FAULTS,
    DispenserOutcome,
    DispenseStep,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    RefundStatus,
    Scope,
    Severity,
    fault_for_outcome,
)
from contracts.vending_machine import EXPECTED_SUBSYSTEMS, SubsystemCapabilities


def test_contract_version():
    # 0.4.0 -> 0.5.0: SVC-102 (new FaultCode, seven-member
    # PAYMENT_BLOCKING_FAULTS) plus part 3's deferred DATA-101 wording bump.
    # 0.6.0 -> 0.7.0: ChannelDescriptor gains direction/driven_by (additive).
    # 0.7.0 -> 0.8.0: CFG-101/CFG-102 (dispenser profiles), additive.
    # 0.8.0 -> 1.0.0: major bump (Copilot review, PR #32) -- wire-breaking:
    # `dispense` params now require mechanism+profile, `cmd/dispense` is
    # removed, and DispenserStatus.request_id is now always set for sales.
    assert CONTRACT_VERSION == "1.0.0"


def test_every_fault_code_has_a_table_entry():
    missing = [c for c in FaultCode if c not in FAULT_TABLE]
    assert missing == []


def test_every_failure_outcome_maps_to_a_fault_code():
    # Every (mechanism, outcome) pair registered in OUTCOME_FAULTS is for a
    # non-complete outcome, and fault_for_outcome returns exactly what the
    # table says -- the helper must never silently diverge from its data.
    for (mechanism, outcome), expected_fault in OUTCOME_FAULTS.items():
        assert outcome is not DispenserOutcome.complete
        assert fault_for_outcome(mechanism, outcome) is expected_fault
    # M1 (whole-branch review): every non-complete outcome is reachable
    # through OUTCOME_FAULTS by *some* mechanism -- none was left
    # unmapped for every mechanism that could report it.
    assert {o for _, o in OUTCOME_FAULTS} == set(DispenserOutcome) - {
        DispenserOutcome.complete
    }


def test_outcome_mapping_matches_spec():
    assert OUTCOME_FAULTS[("bagged_ice", DispenserOutcome.timeout)] is FaultCode.ICE_301
    assert OUTCOME_FAULTS[("bagged_ice", DispenserOutcome.error)] is FaultCode.ICE_302
    assert OUTCOME_FAULTS[("bagged_ice", DispenserOutcome.jam)] is FaultCode.ICE_401
    assert (
        OUTCOME_FAULTS[("bagged_ice", DispenserOutcome.door_open)] is FaultCode.ICE_402
    )
    assert OUTCOME_FAULTS[("water_fill", DispenserOutcome.no_flow)] is FaultCode.WTR_101
    assert (
        OUTCOME_FAULTS[("water_fill", DispenserOutcome.over_dispense)]
        is FaultCode.WTR_102
    )
    assert OUTCOME_FAULTS[("water_fill", DispenserOutcome.timeout)] is FaultCode.WTR_101
    assert OUTCOME_FAULTS[("water_fill", DispenserOutcome.error)] is FaultCode.ICE_302
    assert (
        OUTCOME_FAULTS[("bagged_ice", DispenserOutcome.bin_empty)] is FaultCode.ICE_101
    )
    assert (
        OUTCOME_FAULTS[("water_fill", DispenserOutcome.bin_empty)] is FaultCode.ICE_101
    )
    assert len(OUTCOME_FAULTS) == 10


def test_dispense_step_values():
    assert DispenseStep.agitate == "agitate"
    assert DispenseStep.fill == "fill"
    assert DispenseStep.release == "release"
    assert {s.value for s in DispenseStep} == {"agitate", "fill", "release"}


def test_ice_302_description_is_mechanism_agnostic():
    assert FAULT_TABLE[FaultCode.ICE_302].description == (
        "Dispense actuator fault reported by the board "
        "(motor stall, valve driver, over-current)"
    )


def test_fault_for_outcome_unmapped_raises_keyerror():
    with pytest.raises(KeyError) as exc_info:
        fault_for_outcome("bagged_ice", DispenserOutcome.complete)
    message = str(exc_info.value)
    assert "bagged_ice" in message
    assert "complete" in message


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


class TestSubsystemCapabilities:
    def test_minimal(self):
        caps = SubsystemCapabilities(
            subsystem="vending", firmware="abc1234", contract_version="0.4.0"
        )
        assert caps.brand == "" and caps.model == ""
        assert caps.hardware_id is None and caps.ip is None
        assert caps.channels == [] and caps.commands == []

    def test_full(self):
        caps = SubsystemCapabilities(
            subsystem="mdb",
            firmware="abc1234",
            contract_version="0.4.0",
            brand="Acme",
            model="X1",
            hardware_id="02:11:22:33:44:55",
            ip="192.168.86.40",
            commands=["payment/enable", "refund"],
        )
        data = caps.model_dump(mode="json")
        assert data["hardware_id"] == "02:11:22:33:44:55"
        assert data["commands"] == ["payment/enable", "refund"]

    def test_subsystem_pattern(self):
        with pytest.raises(ValidationError):
            SubsystemCapabilities(
                subsystem="Bad Name", firmware="x", contract_version="0.4.0"
            )

    def test_contract_version_bumped(self):
        assert CONTRACT_VERSION == "1.0.0"

    def test_expected_subsystems(self):
        assert EXPECTED_SUBSYSTEMS == ("vending", "mdb", "ice_maker")


def test_pay_104_is_a_machine_warning():
    from contracts.vending_machine import FAULT_TABLE, FaultCode, Scope, Severity

    spec = FAULT_TABLE[FaultCode.PAY_104]
    assert FaultCode.PAY_104.value == "PAY-104"
    assert spec.severity is Severity.warning
    assert spec.scope is Scope.machine
    assert "restart" in spec.description.lower()


def test_contract_version_bumped_for_new_code():
    from contracts.vending_machine import CONTRACT_VERSION

    assert CONTRACT_VERSION == "1.0.0"


def test_payment_blocking_faults_is_exactly_the_six_hazards_plus_svc_102():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    assert PAYMENT_BLOCKING_FAULTS == frozenset(
        {
            FaultCode.ICE_402,
            FaultCode.WTR_103,
            FaultCode.WTR_104,
            FaultCode.ENV_102,
            FaultCode.ENV_103,
            FaultCode.PWR_102,
            FaultCode.SVC_102,
        }
    )
    assert len(PAYMENT_BLOCKING_FAULTS) == 7


def test_every_payment_blocking_fault_is_machine_scope_and_critical_except_svc_102():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    for code in PAYMENT_BLOCKING_FAULTS:
        spec = FAULT_TABLE[code]
        assert spec.scope is Scope.machine, code
        if code is FaultCode.SVC_102:
            # Deliberately not `critical`: SVC-102 is an operator-held
            # maintenance lease, not a hardware failure, and clears itself
            # when the lease is released — the opposite of `critical`'s
            # "never auto-clears". See contracts/vending_machine.py.
            assert spec.severity is Severity.warning, code
        else:
            assert spec.severity is Severity.critical, code


class TestSvc102:
    def test_exists_with_expected_spec(self):
        spec = FAULT_TABLE[FaultCode.SVC_102]
        assert FaultCode.SVC_102.value == "SVC-102"
        assert spec.scope is Scope.machine
        assert spec.description == "Maintenance test in progress"

    def test_blocks_payment(self):
        from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

        assert FaultCode.SVC_102 in PAYMENT_BLOCKING_FAULTS

    def test_payment_blocking_faults_now_has_seven_members(self):
        from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

        assert len(PAYMENT_BLOCKING_FAULTS) == 7


def test_pay_104_is_a_warning_and_never_blocks_payment():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    spec = FAULT_TABLE[FaultCode.PAY_104]
    assert spec.severity is Severity.warning
    assert spec.scope is Scope.machine
    assert FaultCode.PAY_104 not in PAYMENT_BLOCKING_FAULTS


def test_data_101_is_an_alert_class_machine_warning_for_the_sale_journal():
    spec = FAULT_TABLE[FaultCode.DATA_101]
    assert FaultCode.DATA_101.value == "DATA-101"
    assert spec.severity is Severity.warning
    assert spec.scope is Scope.machine
    assert spec.description == "Sale write failed; held in fallback file"


def test_data_102_is_an_alert_class_machine_warning_for_the_event_db_reset():
    spec = FAULT_TABLE[FaultCode.DATA_102]
    assert FaultCode.DATA_102.value == "DATA-102"
    assert spec.severity is Severity.warning
    assert spec.scope is Scope.machine
    assert (
        spec.description
        == "Event database was reset after corruption; history before the reset is lost"
    )


def test_data_faults_never_block_payment_and_the_original_six_hazards_are_unchanged():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    assert FaultCode.DATA_101 not in PAYMENT_BLOCKING_FAULTS
    assert FaultCode.DATA_102 not in PAYMENT_BLOCKING_FAULTS
    original_six = {
        FaultCode.ICE_402,
        FaultCode.WTR_103,
        FaultCode.WTR_104,
        FaultCode.ENV_102,
        FaultCode.ENV_103,
        FaultCode.PWR_102,
    }
    assert original_six <= PAYMENT_BLOCKING_FAULTS
    # SVC-102 is the one new member this task adds (six -> seven).
    assert len(PAYMENT_BLOCKING_FAULTS) == 7


def test_cfg_faults_are_registered_and_never_block_payment():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    assert FAULT_TABLE[FaultCode.CFG_101].severity is Severity.product_unavailable
    assert FAULT_TABLE[FaultCode.CFG_101].scope is Scope.product
    assert FAULT_TABLE[FaultCode.CFG_102].severity is Severity.warning
    assert FAULT_TABLE[FaultCode.CFG_102].scope is Scope.machine
    assert FaultCode.CFG_101 not in PAYMENT_BLOCKING_FAULTS
    assert FaultCode.CFG_102 not in PAYMENT_BLOCKING_FAULTS
