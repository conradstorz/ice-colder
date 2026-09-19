"""Bounds validation on inbound MQTT payloads (forged-message hardening)."""

import pytest
from pydantic import ValidationError

from config.config_model import ConfigModel
from controller.vmc import VMC
from services.mqtt_messages import ButtonPress, DispenseCommand, PaymentEvent, VMCStatus


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

    def test_dispense_command_rejects_negative_slot(self):
        with pytest.raises(ValidationError):
            DispenseCommand(slot=-1)

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
            await vmc._handle_mqtt_payment(
                "payment/credit", {"amount": -5.0, "method": "cash"}
            )
        assert vmc.credit_escrow == 0.0
