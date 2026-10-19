"""Provider-neutral OIDC pieces shared by ``auth`` and the concrete providers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol


class SignInError(Exception):
    """Sign-in could not be verified; the message is safe to show the user."""


@dataclass(frozen=True)
class IdentityClaims:
    sub: str
    email: str
    email_verified: bool
    name: str


class OidcProvider(Protocol):
    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    async def exchange(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> IdentityClaims: ...


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
