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
from collections.abc import AsyncIterator
from collections import OrderedDict

from fastapi import FastAPI, Request, Response
from loguru import logger
from pydantic import ValidationError

from ice_cloud.body_limit import BUNDLE_PREFIX
from ice_cloud.store.repository import Machine
from ice_contracts.cloud import signing
from ice_contracts.cloud.identity import RegisterMachine

REGISTER_PATH = "/api/v1/machines/register"
# 2 x the 20 MiB bundle cap on a 512 MB host; a third concurrent PUT answers 503.
MAX_INFLIGHT_BUNDLE_BODIES = 2
BUNDLE_RETRY_AFTER_SECONDS = 30


class InflightSlots:
    """A non-blocking counter: ``try_acquire`` never waits."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def try_acquire(self) -> bool:
        if self._used >= self._limit:
            return False
        self._used += 1
        return True

    def release(self) -> None:
        self._used = max(0, self._used - 1)


class BundleBusy(Exception):
    """Too many bundle bodies are being buffered; rendered as 503."""


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

    async def busy(_request: Request, _exc: Exception) -> Response:
        logger.warning("bundle upload refused: too many bodies in flight")
        return Response(
            status_code=503,
            headers={"Retry-After": str(BUNDLE_RETRY_AFTER_SECONDS)},
        )

    app.add_exception_handler(BundleBusy, busy)


def _decode_key(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


async def require_machine(request: Request) -> AsyncIterator[Machine | None]:
    """Authenticate the request (see :func:`_verify`) and hold the bundle-body
    slot, if one was taken, until the request is over.

    A generator dependency so the slot is released on every exit: a failure in
    ``_verify`` itself after acquisition, a handler exception, or success.
    """
    try:
        yield await _verify(request)
    finally:
        if getattr(request.state, "bundle_slot", False):
            request.state.bundle_slot = False
            request.app.state.bundle_bodies.release()


async def _verify(request: Request) -> Machine | None:
    """Verify the signature headers; return the machine, or None for a first
    registration (unknown fingerprint allowed on the register path only).

    Order matters: nothing larger than the JSON cap is read before the caller
    is a known, claimed machine inside its back-off allowance, and a bundle PUT
    additionally holds one of ``MAX_INFLIGHT_BUNDLE_BODIES`` process-wide slots
    (else 503 before the read). Header, timestamp,
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

    # Back-off state exists only for fingerprints already in the database, so
    # a flood of registers with fresh keypairs cannot grow the registry.
    known = machine is not None or (
        is_register and await asyncio.to_thread(app.state.repo.machine, fp) is not None
    )
    backoff = app.state.backoff
    client_ip = backoff.client_ip(request)
    if known and backoff.check("machine", fp, client_ip, trusted=True) is not None:
        raise MachineAuthError("backing off", fp)

    if request.url.path.startswith(BUNDLE_PREFIX):
        if not app.state.bundle_bodies.try_acquire():
            raise BundleBusy
        request.state.bundle_slot = True
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
        if known:
            backoff.record_failure("machine", fp, client_ip, trusted=True)
        raise MachineAuthError("bad signature", fp)
    # Base64 ignores the unused trailing bits, so several header strings decode
    # to one signature. Reject the non-canonical ones and key the replay cache
    # on the decoded bytes so a re-encoding cannot slip past it.
    raw_signature = base64.b64decode(signature, validate=True)
    if base64.b64encode(raw_signature).decode("ascii") != signature:
        raise MachineAuthError("non-canonical signature encoding", fp)
    if known:
        backoff.record_success("machine", fp, client_ip, trusted=True)
    now = app.state.now()
    if not app.state.seen_signatures.check_and_add(
        f"{fp}:{raw_signature.hex()}", now.timestamp()
    ):
        raise MachineAuthError("replayed signature", fp)
    if machine is not None:
        await asyncio.to_thread(app.state.repo.touch_seen, fp, app.state.now())
    return machine
