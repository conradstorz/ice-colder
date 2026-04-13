"""Tests for controller/vmc.py — VMC finite state machine transitions."""
import pytest
from unittest.mock import MagicMock
from config.config_model import ConfigModel
from controller.vmc import VMC


@pytest.fixture
def vmc():
    """Create a VMC instance (no event loop attached)."""
    cfg = ConfigModel()
    v = VMC(config=cfg)
    yield v
    # Cancel any pending async tasks
    for t in v._pending_tasks:
        t.cancel()


class TestInitialState:
    def test_starts_idle(self, vmc):
        assert vmc.state == "idle"

    def test_initial_credit_is_zero(self, vmc):
        assert vmc.credit_escrow == 0.0

    def test_no_selected_product(self, vmc):
        assert vmc.selected_product is None


class TestDeposit:
    def test_deposit_increases_credit(self, vmc):
        vmc.deposit_funds(5.00)
        assert vmc.credit_escrow == 5.00

    def test_deposit_updates_payment_method(self, vmc):
        vmc.deposit_funds(1.00, payment_method="Cash")
        assert vmc.last_payment_method == "Cash"

    def test_multiple_deposits_accumulate(self, vmc):
        vmc.deposit_funds(1.00)
        vmc.deposit_funds(2.50)
        assert vmc.credit_escrow == 3.50


class TestRefund:
    def test_refund_zeroes_credit(self, vmc):
        vmc.deposit_funds(5.00)
        vmc.request_refund()
        assert vmc.credit_escrow == 0.0

    def test_refund_with_no_credit(self, vmc):
        """Refund with no balance should not error."""
        vmc.request_refund()
        assert vmc.credit_escrow == 0.0


class TestFSMTransitions:
    def test_start_interaction(self, vmc):
        vmc.start_interaction()
        assert vmc.state == "interacting_with_user"

    def test_dispense_from_interacting(self, vmc):
        vmc.start_interaction()
        vmc.dispense_product()
        assert vmc.state == "dispensing"

    def test_complete_transaction_to_idle_when_no_credit(self, vmc):
        vmc.start_interaction()
        vmc.dispense_product()
        assert vmc.credit_escrow == 0.0
        vmc.complete_transaction()
        assert vmc.state == "idle"

    def test_complete_transaction_stays_interacting_with_credit(self, vmc):
        vmc.deposit_funds(10.00)
        vmc.start_interaction()
        vmc.dispense_product()
        vmc.complete_transaction()
        assert vmc.state == "interacting_with_user"

    def test_error_from_any_state(self, vmc):
        vmc.error_occurred()
        assert vmc.state == "error"

    def test_error_from_interacting(self, vmc):
        vmc.start_interaction()
        vmc.error_occurred()
        assert vmc.state == "error"

    def test_reset_from_error(self, vmc):
        vmc.error_occurred()
        vmc.reset_state()
        assert vmc.state == "idle"
        assert vmc.selected_product is None

    def test_reset_clears_insufficient_message(self, vmc):
        vmc.last_insufficient_message = "some message"
        vmc.error_occurred()
        vmc.reset_state()
        assert vmc.last_insufficient_message == ""


class TestGetStatus:
    def test_status_contains_expected_keys(self, vmc):
        status = vmc.get_status()
        assert "state" in status
        assert "selected_product" in status
        assert "credit_escrow" in status
        assert "last_payment_method" in status

    def test_status_reflects_state(self, vmc):
        assert vmc.get_status()["state"] == "idle"
        vmc.start_interaction()
        assert vmc.get_status()["state"] == "interacting_with_user"


class TestCallbacks:
    def test_update_callback_called_on_deposit(self, vmc):
        cb = MagicMock()
        vmc.set_update_callback(cb)
        vmc.deposit_funds(1.00)
        cb.assert_called()

    def test_message_callback_called_on_deposit(self, vmc):
        cb = MagicMock()
        vmc.set_message_callback(cb)
        vmc.deposit_funds(1.00)
        cb.assert_called()
