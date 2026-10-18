"""Tests for the MDB payment board <-> edge contract (event stream)."""

from datetime import timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from ice_contracts.board import payment
from ice_contracts.board.payment import (
    CONTRACT_VERSION,
    EVENT_KINDS,
    STATE_REPUBLISH_SECONDS,
    BillReturnReason,
    BillStacked,
    CoinAccepted,
    CoinRoute,
    Device,
    DeviceState,
    DeviceStatus,
    InhibitReason,
    PaymentEventAck,
    PaymentEventEnvelope,
    PaymentState,
    PayoutResult,
    PayoutStatus,
    SessionEndReason,
    TubeLevel,
)

EVENT_ID = "evt-00001"
BOOT_ID = "boot-0001"

MINIMAL_EVENTS = {
    "coin_accepted": {"cents": 25, "routed": "tubes"},
    "bill_escrowed": {"cents": 500},
    "bill_stacked": {"cents": 500},
    "bill_returned": {"cents": 500, "reason": "customer"},
    "escrow_return_pressed": {},
    "payout_result": {
        "request_id": "req-00001",
        "requested_cents": 100,
        "paid_cents": 100,
        "status": "ok",
    },
    "session_begun": {"session_id": "s1", "funds_cents": 1000},
    "vend_approved": {"session_id": "s1", "request_id": "req-00002", "cents": 150},
    "vend_denied": {
        "session_id": "s1",
        "request_id": "req-00003",
        "reason": "no funds",
    },
    "session_ended": {"session_id": "s1", "reason": "complete"},
    "device_status": {"device": "coin", "status": "ready"},
    "cash_collected": {"device": "bill"},
}

EXPECTED_KINDS = (
    "coin_accepted",
    "bill_escrowed",
    "bill_stacked",
    "bill_returned",
    "escrow_return_pressed",
    "payout_result",
    "session_begun",
    "vend_approved",
    "vend_denied",
    "session_ended",
    "device_status",
    "cash_collected",
)


def _envelope(event: dict, **over) -> dict:
    base = {"event_id": EVENT_ID, "boot_id": BOOT_ID, "seq": 1, "event": event}
    base.update(over)
    return base


def test_contract_version():
    assert CONTRACT_VERSION == "1.0.0"


def test_topics():
    assert payment.PAYMENT_EVENT_TOPIC == "payment/event"
    assert payment.PAYMENT_EVENT_ACK_TOPIC == "payment/event/ack"
    assert payment.PAYMENT_STATE_TOPIC == "payment/state"


class TestCents:
    def test_valid_int(self):
        c = CoinAccepted(kind="coin_accepted", cents=25, routed="tubes")
        assert c.cents == 25

    def test_kind_defaults_and_is_emitted(self):
        c = CoinAccepted(cents=25, routed="tubes")
        assert c.kind == "coin_accepted"
        assert c.model_dump(mode="json")["kind"] == "coin_accepted"

    def test_float_rejected(self):
        with pytest.raises(ValidationError):
            CoinAccepted(kind="coin_accepted", cents=25.0, routed="tubes")

    def test_numeric_string_rejected(self):
        with pytest.raises(ValidationError):
            CoinAccepted(kind="coin_accepted", cents="25", routed="tubes")

    def test_zero_rejected_for_positive(self):
        with pytest.raises(ValidationError):
            CoinAccepted(kind="coin_accepted", cents=0, routed="tubes")

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            BillStacked(kind="bill_stacked", cents=-100)


class TestEventUnion:
    @pytest.mark.parametrize("kind", EXPECTED_KINDS)
    def test_round_trip(self, kind):
        event = {"kind": kind, **MINIMAL_EVENTS[kind]}
        env = PaymentEventEnvelope.model_validate(_envelope(event))
        assert env.event.kind == kind
        dumped = env.model_dump(mode="json")
        assert dumped["event"]["kind"] == kind
        again = PaymentEventEnvelope.model_validate_json(env.model_dump_json())
        assert again.event.kind == kind
        assert again.event == env.event
        for key, value in dumped["event"].items():
            if (key.endswith("cents") or key == "mdb_status") and value is not None:
                assert type(value) is int

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventEnvelope.model_validate(
                _envelope({"kind": "coin_rejected", "cents": 25})
            )

    def test_event_kinds_order(self):
        assert EVENT_KINDS == EXPECTED_KINDS

    def test_event_kinds_match_union_members(self):
        union = get_args(payment.PaymentEventBody)[0]
        members = get_args(union)
        assert {m.model_fields["kind"].default for m in members} == set(EVENT_KINDS)
        assert len(members) == len(EVENT_KINDS) == 12


class TestEnvelope:
    EVENT = {"kind": "escrow_return_pressed"}

    def test_valid(self):
        env = PaymentEventEnvelope.model_validate(_envelope(self.EVENT))
        assert env.seq == 1

    def test_seq_zero_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventEnvelope.model_validate(_envelope(self.EVENT, seq=0))

    def test_seq_float_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventEnvelope.model_validate(_envelope(self.EVENT, seq=1.0))

    def test_short_event_id_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventEnvelope.model_validate(
                _envelope(self.EVENT, event_id="1234567")
            )

    def test_long_boot_id_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventEnvelope.model_validate(_envelope(self.EVENT, boot_id="b" * 33))

    def test_ts_defaults_to_utc(self):
        env = PaymentEventEnvelope.model_validate(_envelope(self.EVENT))
        assert env.ts.tzinfo is not None
        assert env.ts.utcoffset() == timedelta(0)


class TestPayoutResultConsistency:
    def _make(self, status, requested, paid):
        return PayoutResult(
            request_id="req-00001",
            requested_cents=requested,
            paid_cents=paid,
            status=status,
        )

    def test_valid_examples(self):
        self._make("ok", 100, 100)
        self._make("partial", 100, 40)
        self._make("failed", 100, 0)

    def test_partial_with_zero_paid_is_valid(self):
        self._make("partial", 100, 0)

    def test_ok_mismatch_rejected(self):
        with pytest.raises(ValidationError) as exc:
            self._make("ok", 100, 50)
        assert "\n" not in exc.value.errors()[0]["msg"]

    def test_partial_full_payment_rejected(self):
        with pytest.raises(ValidationError) as exc:
            self._make("partial", 100, 100)
        assert "\n" not in exc.value.errors()[0]["msg"]

    def test_failed_with_paid_rejected(self):
        with pytest.raises(ValidationError) as exc:
            self._make("failed", 100, 10)
        assert "\n" not in exc.value.errors()[0]["msg"]


class TestEnums:
    def test_device(self):
        assert {m.value for m in Device} == {"coin", "bill", "cashless"}

    def test_device_status(self):
        assert {m.value for m in DeviceStatus} == {
            "ready",
            "disabled",
            "busy",
            "jammed",
            "full",
            "empty",
            "tilt",
            "offline",
            "error",
        }

    def test_coin_route(self):
        assert {m.value for m in CoinRoute} == {"tubes", "cashbox"}

    def test_bill_return_reason(self):
        assert {m.value for m in BillReturnReason} == {
            "policy",
            "edge_silent",
            "customer",
            "edge",
            "timeout",
        }

    def test_payout_status(self):
        assert {m.value for m in PayoutStatus} == {"ok", "partial", "failed"}

    def test_session_end_reason(self):
        assert {m.value for m in SessionEndReason} == {
            "complete",
            "cancelled",
            "reader",
            "edge",
            "timeout",
        }


class TestAck:
    def test_valid(self):
        ack = PaymentEventAck(boot_id=BOOT_ID, seq=3)
        assert ack.seq == 3
        assert ack.timestamp.tzinfo is not None

    def test_seq_zero_rejected(self):
        with pytest.raises(ValidationError):
            PaymentEventAck(boot_id=BOOT_ID, seq=0)


def state_dict() -> dict:
    return {
        "boot_id": BOOT_ID,
        "seq_last": 7,
        "currency": "USD",
        "policy_id": "pol-1",
        "accepting": True,
        "inhibited_reason": None,
        "escrow_cents": 150,
        "bill_in_escrow_cents": 500,
        "devices": {
            "coin": {
                "present": True,
                "status": "ready",
                "level": 3,
                "model": "C1",
                "serial": "SN1",
                "denominations": [5, 10, 25, 100],
                "enabled": [5, 10, 25],
            },
            "bill": {
                "present": True,
                "status": "ready",
                "denominations": [100, 500, 1000],
                "enabled": [100, 500],
            },
            "cashless": {"present": False, "status": "offline"},
        },
        "tubes": [
            {"cents": 5, "count": 10, "full": False},
            {"cents": 25, "count": 0, "full": False},
        ],
        "exact_change": False,
        "stacker": {"count": 12, "full": False},
        "session": {
            "session_id": "s1",
            "funds_cents": 1000,
            "started_ts": "2026-01-01T00:00:00Z",
        },
    }


class TestPaymentState:
    def test_valid_round_trip_keeps_ints(self):
        st = PaymentState.model_validate(state_dict())
        again = PaymentState.model_validate_json(st.model_dump_json())
        assert again == st
        dumped = again.model_dump(mode="json")
        assert type(dumped["escrow_cents"]) is int
        assert type(dumped["bill_in_escrow_cents"]) is int
        assert type(dumped["tubes"][0]["cents"]) is int
        assert type(dumped["session"]["funds_cents"]) is int
        assert dumped["devices"]["coin"]["enabled"] == [5, 10, 25]

    def test_currency_other_rejected(self):
        with pytest.raises(ValidationError):
            PaymentState.model_validate({**state_dict(), "currency": "EUR"})

    def test_accepting_with_reason_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PaymentState.model_validate(
                {**state_dict(), "accepting": True, "inhibited_reason": "policy"}
            )
        assert "\n" not in exc.value.errors()[0]["msg"]

    def test_not_accepting_without_reason_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PaymentState.model_validate(
                {**state_dict(), "accepting": False, "inhibited_reason": None}
            )
        assert "\n" not in exc.value.errors()[0]["msg"]

    def test_not_accepting_with_reason_valid(self):
        st = PaymentState.model_validate(
            {**state_dict(), "accepting": False, "inhibited_reason": "edge_silent"}
        )
        assert st.inhibited_reason == InhibitReason.edge_silent

    def test_float_escrow_rejected(self):
        with pytest.raises(ValidationError):
            PaymentState.model_validate({**state_dict(), "escrow_cents": 1.5})

    def test_seq_last_zero_valid(self):
        assert (
            PaymentState.model_validate({**state_dict(), "seq_last": 0}).seq_last == 0
        )

    def test_short_boot_id_rejected(self):
        with pytest.raises(ValidationError):
            PaymentState.model_validate({**state_dict(), "boot_id": "short"})

    def test_nullable_fields(self):
        st = PaymentState.model_validate(
            {
                **state_dict(),
                "policy_id": None,
                "bill_in_escrow_cents": None,
                "stacker": None,
                "session": None,
            }
        )
        assert st.session is None and st.stacker is None

    def test_ts_defaults_to_utc(self):
        st = PaymentState.model_validate(state_dict())
        assert st.ts.utcoffset() == timedelta(0)

    def test_republish_constant(self):
        assert STATE_REPUBLISH_SECONDS == 30


class TestDeviceState:
    def test_enabled_not_subset_rejected(self):
        with pytest.raises(ValidationError) as exc:
            DeviceState(
                present=True,
                status="ready",
                denominations=[100, 200],
                enabled=[500],
            )
        assert "\n" not in exc.value.errors()[0]["msg"]

    def test_level_four_rejected(self):
        with pytest.raises(ValidationError):
            DeviceState(present=True, status="ready", level=4)

    def test_defaults(self):
        d = DeviceState(present=False, status="offline")
        assert d.level is None and d.model is None and d.serial is None
        assert d.denominations == [] and d.enabled == []


class TestTubeLevel:
    def test_negative_count_rejected(self):
        with pytest.raises(ValidationError):
            TubeLevel(cents=25, count=-1, full=False)

    def test_zero_cents_rejected(self):
        with pytest.raises(ValidationError):
            TubeLevel(cents=0, count=1, full=False)


class TestInhibitReason:
    def test_values(self):
        assert {m.value for m in InhibitReason} == {
            "no_policy",
            "edge_silent",
            "policy",
            "device_fault",
        }
