"""Google as an OIDC provider: discovery, JWKS and RS256 ID-token verification.

The ID token is verified with ``cryptography``; there is no JWT library.
"""

from __future__ import annotations

import binascii
import hmac
import json
import time
from collections.abc import Callable
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from ice_cloud.oidc import IdentityClaims, SignInError, b64d

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class GoogleProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._clock = clock
        self._discovery: dict | None = None
        self._jwks: dict[str, dict] = {}

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GOOGLE_AUTHORIZE_URL}?{query}"

    async def _get_json(self, url: str) -> dict:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignInError("Google could not be reached") from exc
        if not isinstance(data, dict):
            raise SignInError("Google returned an unexpected response")
        return data

    async def _discover(self) -> dict:
        if self._discovery is None:
            self._discovery = await self._get_json(GOOGLE_DISCOVERY_URL)
        return self._discovery

    async def _key_for(self, kid: str) -> dict:
        if kid not in self._jwks:
            await self._refresh_jwks()
        if kid not in self._jwks:
            raise SignInError("Unknown signing key")
        return self._jwks[kid]

    async def _refresh_jwks(self) -> None:
        doc = await self._discover()
        uri = doc.get("jwks_uri")
        if not isinstance(uri, str):
            raise SignInError("Google discovery is incomplete")
        keys = (await self._get_json(uri)).get("keys", [])
        self._jwks = {k["kid"]: k for k in keys if isinstance(k, dict) and "kid" in k}

    async def exchange(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> IdentityClaims:
        doc = await self._discover()
        endpoint = doc.get("token_endpoint")
        if not isinstance(endpoint, str):
            raise SignInError("Google discovery is incomplete")
        try:
            response = await self._http.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code_verifier": code_verifier,
                },
            )
            response.raise_for_status()
            id_token = response.json().get("id_token")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SignInError("Google refused the sign-in") from exc
        if not isinstance(id_token, str):
            raise SignInError("Google returned no ID token")
        return await self._verify(id_token, nonce)

    async def _verify(self, token: str, nonce: str) -> IdentityClaims:
        parts = token.split(".")
        if len(parts) != 3:
            raise SignInError("Malformed ID token")
        try:
            header = json.loads(b64d(parts[0]))
            claims = json.loads(b64d(parts[1]))
            signature = b64d(parts[2])
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise SignInError("Malformed ID token") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise SignInError("Malformed ID token")
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise SignInError("Unsupported ID token")
        jwk = await self._key_for(header["kid"])
        try:
            public = RSAPublicNumbers(
                int.from_bytes(b64d(jwk["e"]), "big"),
                int.from_bytes(b64d(jwk["n"]), "big"),
            ).public_key()
            public.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (
            InvalidSignature,
            KeyError,
            TypeError,
            AttributeError,
            ValueError,
            binascii.Error,
        ) as exc:
            raise SignInError("ID token signature is invalid") from exc

        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise SignInError("ID token issuer is wrong")
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self._client_id not in audiences:
            raise SignInError("ID token audience is wrong")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or exp <= self._clock():
            raise SignInError("ID token has expired")
        given = claims.get("nonce")
        if not isinstance(given, str) or not hmac.compare_digest(
            given.encode(), nonce.encode()
        ):
            raise SignInError("ID token nonce is wrong")
        verified = claims.get("email_verified")
        if verified is not True and verified != "true":
            raise SignInError("Your Google email address is not verified")
        sub, email = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not sub or not isinstance(email, str):
            raise SignInError("ID token is missing the account identity")
        name = claims.get("name")
        return IdentityClaims(
            sub=sub,
            email=email,
            email_verified=True,
            name=name if isinstance(name, str) and name else email,
        )
