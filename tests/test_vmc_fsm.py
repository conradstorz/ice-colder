"""Tests for ice_edge/sale/vmc.py — VMC finite state machine transitions."""

from unittest.mock import MagicMock

import pytest

from ice_common.config_model import ConfigModel
from ice_edge.app.machine import Machine
from ice_edge.sale.vmc import InvalidTransition


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
        """Update callback receives state, product, and credit_escrow."""
        cb = MagicMock()
        vmc.deposit_funds(2.00)  # Seed escrow to distinguish amount from balance
        vmc.outputs.set_update_callback(cb)
        vmc.deposit_funds(1.00)
        # Callback is called once with (state, product, credit), not twice
        cb.assert_called_once_with("idle", None, 3.0)

    def test_message_callback_called_on_deposit(self, vmc):
        """Message callback receives formatted text with amount and balance."""
        cb = MagicMock()
        vmc.deposit_funds(2.00)  # Seed escrow to distinguish amount from balance
        vmc.outputs.set_message_callback(cb)
        vmc.deposit_funds(1.00)
        # Callback is called with the exact message text
        cb.assert_called_once_with("$1.00 deposited. Current balance: $3.00.")


class TestEventRecorder:
    def test_error_transition_calls_recorder(self):
        config = ConfigModel()
        machine = Machine(config=config)
        vmc = machine.vmc
        recorder = MagicMock()
        machine.set_event_recorder(recorder)
        vmc.error_occurred()
        recorder.record.assert_called_once_with("error", value=1.0)
        machine.cancel_pending_tasks()


class TestExplicitTriggers:
    def test_invalid_trigger_raises_and_keeps_state(self, vmc):
        vmc.set_state("interacting_with_user")
        with pytest.raises(InvalidTransition):
            vmc.start_interaction()
        assert vmc.state == "interacting_with_user"

    @pytest.mark.parametrize(
        "start", ["idle", "interacting_with_user", "dispensing", "error"]
    )
    def test_error_occurred_from_every_state(self, vmc, start):
        vmc.set_state(start)
        vmc.error_occurred()
        assert vmc.state == "error"

    @pytest.mark.parametrize(
        ("credit", "expected"), [(5.0, "interacting_with_user"), (0.0, "idle")]
    )
    def test_complete_transaction_destination_follows_credit(
        self, vmc, credit, expected
    ):
        vmc.credit_escrow = credit
        vmc.set_state("dispensing")
        vmc.complete_transaction()
        assert vmc.state == expected

    def test_set_state_runs_no_observer(self, vmc):
        seen = MagicMock()
        vmc.subscribe_state_change(seen)
        vmc.set_state("dispensing")
        assert vmc.state == "dispensing"
        seen.assert_not_called()

    def test_reset_state_from_idle_raises(self, vmc):
        with pytest.raises(InvalidTransition):
            vmc.reset_state()

    def test_set_state_rejects_unknown_state(self, vmc):
        with pytest.raises(ValueError):
            vmc.set_state("bogus")
