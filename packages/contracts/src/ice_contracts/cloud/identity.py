"""Machine identity wire models (edge-cloud contract 0.2.0): registration and claim codes."""

import base64
import binascii

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

_ED25519_PUBLIC_KEY_BYTES = 32


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegisterMachine(_Model):
    """Body of POST /api/v1/machines/register."""

    public_key_b64: str
    label: str = Field(min_length=1, max_length=64)

    @field_validator("public_key_b64")
    @classmethod
    def _key_is_32_bytes(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("public_key_b64 is not valid base64") from exc
        if len(raw) != _ED25519_PUBLIC_KEY_BYTES:
            raise ValueError(
                f"public key must be {_ED25519_PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
            )
        return value


class RegisterResponse(_Model):
    fingerprint: str
    claimed: bool
    alerts_handled: bool = False


class ClaimCodeNotice(_Model):
    """Body of POST /api/v1/machines/{fingerprint}/claim-code.

    expires_at None means a setup code, which never expires.
    """

    claim_code_hash: str = Field(min_length=1, max_length=256)
    expires_at: AwareDatetime | None
