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
import threading
from collections import OrderedDict

from fastapi import FastAPI, Request, Response
from loguru import logger
from pydantic import ValidationError

from ice_cloud.body_limit import BUNDLE_PREFIX
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


class SeenSignatures:
    """Recently accepted ``(fingerprint, signature)`` keys, to refuse replays.

    Per-process and lost on restart; the timestamp freshness window bounds the
    exposure. Entries expire after ``window_seconds`` and the oldest is evicted
    beyond ``max_entries``.
    """

    def __init__(self, window_seconds: float, max_entries: int = 10_000) -> None:
        self._window = window_seconds
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, key: str, now: float) -> bool:
        """Record *key*; ``False`` when it was already seen and not expired."""
        with self._lock:
            while self._seen:
                oldest = next(iter(self._seen))
                if now - self._seen[oldest] <= self._window:
                    break
                del self._seen[oldest]
            if key in self._seen:
                return False
            self._seen[key] = now
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True


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

    Order matters: nothing larger than the JSON cap is read before the caller
    is a known, claimed machine inside its back-off allowance. Header, timestamp,
    machine lookup, stored key, path fingerprint, claimed-for-bundles and the
    per-(fingerprint, client) back-off all run before ``request.body()``. Only a
    bad signature counts toward back-off (not an unknown machine, a stale
    timestamp or a refusal), so a drifting clock cannot lock a machine out.
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

    machine: Machine | None = None
    key: bytes | None = None
    is_register = request.url.path == REGISTER_PATH
    if not is_register:
        machine = await asyncio.to_thread(app.state.repo.machine, fp)
        if machine is None:
            raise MachineAuthError("unknown machine", fp)
        key = _decode_key(machine.public_key_b64)
        if key is None:
            raise MachineAuthError("stored key unreadable", fp)
        path_fp = request.path_params.get("fingerprint")
        if path_fp is not None and path_fp != fp:
            raise MachineAuthError("path fingerprint mismatch", fp)
        if request.url.path.startswith(BUNDLE_PREFIX) and machine.owner_id is None:
            raise MachineAuthError("bundle from unclaimed machine", fp)

    backoff = app.state.backoff
    client_ip = backoff.client_ip(request)
    if backoff.check("machine", fp, client_ip, trusted=True) is not None:
        raise MachineAuthError("backing off", fp)

    body = await request.body()
    if is_register:
        try:
            reg = RegisterMachine.model_validate_json(body)
        except ValidationError:
            raise MachineAuthError("invalid register body", fp) from None
        key = _decode_key(reg.public_key_b64)
        if key is None or signing.fingerprint(key) != fp:
            raise MachineAuthError("header fingerprint does not match key", fp)
    assert key is not None

    message = signing.canonical_message(
        request.method, request.url.path, timestamp, body
    )
    if not signing.verify(key, message, signature):
        backoff.record_failure("machine", fp, client_ip, trusted=True)
        raise MachineAuthError("bad signature", fp)
    backoff.record_success("machine", fp, client_ip, trusted=True)
    now = app.state.now()
    if not app.state.seen_signatures.check_and_add(
        f"{fp}:{signature}", now.timestamp()
    ):
        raise MachineAuthError("replayed signature", fp)
    if machine is not None:
        await asyncio.to_thread(app.state.repo.touch_seen, fp, app.state.now())
    return machine
