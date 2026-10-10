"""Bounds validation on inbound MQTT payloads (forged-message hardening)."""

import pytest
from pydantic import ValidationError

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.dispensers import validate_document
from services.mqtt_messages import ButtonPress, DispenseCommand, PaymentEvent, VMCStatus
from tests.dispenser_fixtures import GOOD, ICE, WATER


class TestPaymentEventBounds:
    def test_rejects_negative_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=-5.0, method="cash")

    def test_rejects_zero_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=0.0, method="cash")

    def test_rejects_huge_amount(self):
        with pytest.raises(ValidationError):
            PaymentEvent(amount=10_000.0, method="cash")

    def test_accepts_normal_amount(self):
        assert PaymentEvent(amount=2.50, method="cash").amount == 2.50


class TestOtherMessageBounds:
    def test_button_press_rejects_negative_index(self):
        with pytest.raises(ValidationError):
            ButtonPress(button=-1)

    def test_dispense_command_requires_mechanism_and_profile(self):
        report = validate_document(GOOD, [ICE, WATER])
        profile = report.profiles[1]
        with pytest.raises(ValidationError):
            DispenseCommand(slot=1)
        with pytest.raises(ValidationError):
            DispenseCommand(slot=1, mechanism="bagged_ice")
        with pytest.raises(ValidationError):
            DispenseCommand(slot=1, profile=profile)

    def test_dispense_command_rejects_mechanism_profile_mismatch(self):
        report = validate_document(GOOD, [ICE, WATER])
        profile = report.profiles[1]  # bagged_ice
        with pytest.raises(ValidationError):
            DispenseCommand(slot=1, mechanism="water_fill", profile=profile)

    def test_dispense_command_round_trips_profile(self):
        report = validate_document(GOOD, [ICE, WATER])
        profile = report.profiles[1]
        cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=profile)
        dumped = cmd.model_dump(mode="json")
        again = DispenseCommand.model_validate(dumped)
        assert again == cmd

    def test_vmc_status_has_version(self):
        assert isinstance(VMCStatus(state="idle").version, str)


class TestVMCDepositGuard:
    def test_deposit_ignores_negative(self):
        vmc = VMC(config=ConfigModel())
        vmc.deposit_funds(-1.0)
        assert vmc.credit_escrow == 0.0

    def test_deposit_ignores_zero(self):
        vmc = VMC(config=ConfigModel())
        vmc.deposit_funds(0.0)
        assert vmc.credit_escrow == 0.0

    async def test_mqtt_handler_drops_invalid_payment(self):
        import asyncio

        vmc = VMC(config=ConfigModel())
        vmc.attach_to_loop(asyncio.get_running_loop())
        with pytest.raises(ValidationError):
            await vmc.on_payment_credit(
                "payment/credit", {"amount": -5.0, "method": "cash"}
            )
        assert vmc.credit_escrow == 0.0
