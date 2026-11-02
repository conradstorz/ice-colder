"""MDB firmware golden JSON validates against the payment contract models (pure file checks).

Always runs: no broker, no flag. Pydantic ignores extra keys, so each golden's exact key set
is asserted separately.
"""

import typing

import pytest

from ice_contracts.board import payment
from ice_contracts.board.common import STANDARD_COMMANDS
from ice_contracts.board.payment import PaymentEventEnvelope, PaymentState
from ice_contracts.board.vending_machine import SubsystemCapabilities
from tests.firmware import golden_common
from tests.firmware.golden_common import (
    COMMON_MODELS,
    check_validates_with_exact_keys,
)
from tests.repo_root import REPO_ROOT

GOLDEN = REPO_ROOT / "firmware" / "mdb" / "test" / "golden"

ENVELOPE_KEYS = {"event_id", "boot_id", "seq", "ts", "event"}
# golden file stem -> (model, exact key set)
MODELS = COMMON_MODELS
STATE_STEMS = ("state", "state_inhibited")
STATE_KEYS = set(PaymentState.model_fields)
DEVICE_KEYS = {
    "present",
    "status",
    "level",
    "model",
    "serial",
    "denominations",
    "enabled",
}


def _body_models() -> dict[str, type]:
    union = typing.get_args(payment.PaymentEventBody)[0]
    return {
        m.model_fields["kind"].annotation.__args__[0]: m for m in typing.get_args(union)
    }


BODY_MODELS = _body_models()


def load(stem: str) -> dict:
    return golden_common.load(GOLDEN, stem)


def test_every_golden_file_is_covered():
    expected = (
        set(MODELS)
        | set(STATE_STEMS)
        | {f"event_{kind}" for kind in payment.EVENT_KINDS}
    )
    assert {p.stem for p in GOLDEN.glob("*.json")} == expected


def test_all_twelve_event_kinds_have_a_body_model():
    assert set(BODY_MODELS) == set(payment.EVENT_KINDS)
    assert len(BODY_MODELS) == 12


@pytest.mark.parametrize("kind", payment.EVENT_KINDS)
def test_event_golden_validates_with_exact_keys(kind):
    data = load(f"event_{kind}")
    env = PaymentEventEnvelope.model_validate(data)
    assert set(data) == ENVELOPE_KEYS
    assert env.event.kind == kind
    assert set(data["event"]) == set(BODY_MODELS[kind].model_fields)
    assert data["event_id"] == f"{data['boot_id']}-{data['seq']}"
    for value in data["event"].values():
        assert not isinstance(value, float)


@pytest.mark.parametrize("stem", sorted(MODELS))
def test_golden_validates_with_exact_keys(stem):
    check_validates_with_exact_keys(MODELS, GOLDEN, stem)


@pytest.mark.parametrize("stem", STATE_STEMS)
def test_state_golden_validates_with_exact_keys(stem):
    data = load(stem)
    state = PaymentState.model_validate(data)
    assert set(data) == STATE_KEYS
    assert state.currency == "USD"
    assert state.accepting == (state.inhibited_reason is None)
    for name, device in data["devices"].items():
        assert set(device) == DEVICE_KEYS, name
        assert set(device["enabled"]) <= set(device["denominations"])
    assert all(t["cents"] > 0 for t in data["tubes"])
    if data["session"] is not None:
        assert set(data["session"]) == {"session_id", "funds_cents", "started_ts"}


def test_state_goldens_cover_accepting_and_inhibited():
    ok, inhibited = load("state"), load("state_inhibited")
    assert ok["accepting"] is True and ok["inhibited_reason"] is None
    assert inhibited["accepting"] is False
    assert inhibited["inhibited_reason"] == "edge_silent"
    assert inhibited["policy_id"] is None and inhibited["stacker"] is None


def test_capabilities_golden_lists_exactly_the_payment_commands():
    data = load("capabilities")
    caps = SubsystemCapabilities.model_validate(data)
    assert caps.subsystem == "mdb"
    assert caps.channels == []
    assert set(caps.commands) == set(payment.PAYMENT_COMMANDS) | set(STANDARD_COMMANDS)
    assert len(caps.commands) == 11
    assert caps.contract_version == payment.CONTRACT_VERSION == "1.1.0"
    assert data["commands"][:3] == list(STANDARD_COMMANDS)


def test_heartbeat_and_lwt_goldens():
    assert load("heartbeat")["subsystem"] == "mdb"
    assert load("lwt") == {"subsystem": "mdb", "uptime_seconds": -1}
