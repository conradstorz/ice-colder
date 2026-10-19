"""Machine authentication for the signed machine API (spec sections 4.3, 4.4).

Every ``/api/v1/machines/*`` route and the UI-bundle PUT depend on
:func:`require_machine`. The signature covers the request path exactly as the
app sees it (``request.url.path``): the edge signs the relative ``/api/v1/...``
path, so a deployment behind a path prefix must strip that prefix at the proxy.
"""

from __future__ import annotations

import asyncio
import base64
import binascii

from fastapi import FastAPI, Request, Response
from loguru import logger
from pydantic import ValidationError

from ice_cloud.store.repository import Machine
from ice_contracts.cloud import signing
from ice_contracts.cloud.identity import RegisterMachine

REGISTER_PATH = "/api/v1/machines/register"


def _printable(fp: str | None) -> str:
    """Header-supplied text is attacker-controlled: cap it and drop control chars."""
    if not fp:
        return "-"
    return "".join(c for c in fp[:64] if c.isprintable()) or "-"


class MachineAuthError(Exception):
    """Authentication failed; rendered as 401 with an empty body."""

    def __init__(self, reason: str, fingerprint: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fingerprint = fingerprint


def install(app: FastAPI) -> None:
    """Register the empty-body 401 handler for authentication failures."""

    async def handler(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, MachineAuthError)
        logger.warning(
            "machine auth failed: fingerprint={} reason={}",
            _printable(exc.fingerprint),
            exc.reason,
        )
        return Response(status_code=401)

    app.add_exception_handler(MachineAuthError, handler)


def _decode_key(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


async def require_machine(request: Request) -> Machine | None:
    """Verify the signature headers; return the machine, or None for a first
    registration (unknown fingerprint allowed on the register path only).
    """
    headers = request.headers
    fp = headers.get(signing.HEADER_MACHINE)
    timestamp = headers.get(signing.HEADER_TIMESTAMP)
    signature = headers.get(signing.HEADER_SIGNATURE)
    if not fp or not timestamp or not signature:
        raise MachineAuthError("missing signature headers", fp)
    app = request.app
    if not signing.is_timestamp_fresh(timestamp, app.state.now()):
        raise MachineAuthError("stale or malformed timestamp", fp)
    body = await request.body()

    machine: Machine | None = None
    if request.url.path == REGISTER_PATH:
        try:
            reg = RegisterMachine.model_validate_json(body)
        except ValidationError:
            raise MachineAuthError("invalid register body", fp) from None
        key = _decode_key(reg.public_key_b64)
        if key is None or signing.fingerprint(key) != fp:
            raise MachineAuthError("header fingerprint does not match key", fp)
    else:
        machine = await asyncio.to_thread(app.state.repo.machine, fp)
        if machine is None:
            raise MachineAuthError("unknown machine", fp)
        key = _decode_key(machine.public_key_b64)
        if key is None:
            raise MachineAuthError("stored key unreadable", fp)
        path_fp = request.path_params.get("fingerprint")
        if path_fp is not None and path_fp != fp:
            raise MachineAuthError("path fingerprint mismatch", fp)

    message = signing.canonical_message(
        request.method, request.url.path, timestamp, body
    )
    if not signing.verify(key, message, signature):
        raise MachineAuthError("bad signature", fp)
    if machine is not None:
        await asyncio.to_thread(app.state.repo.touch_seen, fp, app.state.now())
    return machine
