"""Edge-cloud identity models (contract 0.2.0)."""

import base64

import pytest
from pydantic import ValidationError

from ice_contracts.cloud.identity import (
    ClaimCodeNotice,
    RegisterMachine,
    RegisterResponse,
)

KEY = base64.b64encode(bytes(32)).decode()


def test_register_machine_accepts_32_byte_key():
    m = RegisterMachine(public_key_b64=KEY, label="Lobby")
    assert m.label == "Lobby"


@pytest.mark.parametrize(
    "key",
    [
        base64.b64encode(bytes(31)).decode(),
        base64.b64encode(bytes(33)).decode(),
        "!!!notb64",
        "",
    ],
)
def test_register_machine_rejects_short_key(key):
    with pytest.raises(ValidationError):
        RegisterMachine(public_key_b64=key, label="x")


@pytest.mark.parametrize("label", ["", "x" * 65])
def test_register_machine_label_bounds(label):
    with pytest.raises(ValidationError):
        RegisterMachine(public_key_b64=KEY, label=label)


def test_register_machine_forbids_extra():
    with pytest.raises(ValidationError):
        RegisterMachine(public_key_b64=KEY, label="x", extra=1)


def test_register_response_frozen_and_forbids_extra():
    r = RegisterResponse(fingerprint="ab", claimed=False)
    with pytest.raises(ValidationError):
        r.claimed = True
    with pytest.raises(ValidationError):
        RegisterResponse(fingerprint="ab", claimed=False, extra=1)


def test_claim_code_notice_requires_aware_datetime():
    with pytest.raises(ValidationError):
        ClaimCodeNotice(claim_code_hash="h", expires_at="2026-10-19T12:00:00")
    n = ClaimCodeNotice(claim_code_hash="h", expires_at="2026-10-19T12:00:00Z")
    assert n.expires_at is not None


def test_claim_code_notice_none_expiry_and_hash_bounds():
    assert ClaimCodeNotice(claim_code_hash="h", expires_at=None).expires_at is None
    for bad in ("", "x" * 257):
        with pytest.raises(ValidationError):
            ClaimCodeNotice(claim_code_hash=bad, expires_at=None)


def test_register_response_defaults_alerts_handled_false():
    r = RegisterResponse.model_validate({"fingerprint": "ab", "claimed": True})
    assert r.alerts_handled is False


def test_register_response_round_trips_alerts_handled():
    r = RegisterResponse(fingerprint="ab", claimed=True, alerts_handled=True)
    assert RegisterResponse.model_validate_json(r.model_dump_json()) == r
    assert r.alerts_handled is True
