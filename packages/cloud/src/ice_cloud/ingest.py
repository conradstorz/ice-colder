"""Ingest API: machines push reports and UI bundles (cloud spec sections 3, 7)."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger

from ice_cloud.alerts.engine import AlertEvent, diff_reports
from ice_cloud.alerts.handled import alerts_handled_for
from ice_cloud.machine_auth import require_machine
from ice_cloud.store.repository import AlertState, Machine
from ice_contracts.cloud.identity import (
    ClaimCodeNotice,
    RegisterMachine,
    RegisterResponse,
    ReportResponse,
)
from ice_contracts.cloud.report import MachineReport
from ice_contracts.cloud.signing import HEADER_MACHINE
from ice_ui.bundle import BundleError

MAX_BUNDLES_PER_MACHINE = 4  # distinct digests per machine within BUNDLE_WINDOW
BUNDLE_WINDOW = timedelta(days=30)
# Defaults; the deployed values come from CloudSettings (env-overridable).
MAX_UNCLAIMED_MACHINES = 200
UNCLAIMED_MACHINE_TTL_DAYS = 7
MAX_UNCLAIMED_REPORT_BYTES = 16 * 1024
CAP_RETRY_AFTER_SECONDS = 3600
CAP_LOG_INTERVAL = timedelta(seconds=60)
MAX_NEW_REGISTRATIONS_PER_HOUR = 10  # new machines per client address, rolling
REGISTRATION_WINDOW_SECONDS = 3600.0


class SlidingWindow:
    """At most *limit* events per key within the trailing *window_seconds*.

    ``allow`` is a pure check; ``record`` charges an event. Old stamps are pruned
    and empty keys dropped, so memory is bounded by recently active clients.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._stamps: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float] | None:
        stamps = self._stamps.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= self._window:
            stamps.popleft()
        if not stamps:
            del self._stamps[key]
            return None
        return stamps

    def allow(self, key: str, now: float) -> float | None:
        """Seconds until another event is allowed, or None if allowed now."""
        stamps = self._prune(key, now)
        if stamps is None or len(stamps) < self._limit:
            return None
        return stamps[0] + self._window - now

    def record(self, key: str, now: float) -> None:
        self._prune(key, now)
        self._stamps.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        self._stamps.pop(key, None)


router = APIRouter(prefix="/api/v1")


def _log_cap_reached(state, unclaimed: int) -> None:
    now = state.now()
    last = getattr(state, "cap_logged_at", None)
    if last is None or now - last >= CAP_LOG_INTERVAL:
        state.cap_logged_at = now
        logger.warning(
            "Unclaimed machine cap reached ({}); refusing new registrations", unclaimed
        )


async def _admit_new(request: Request, fp: str) -> Response | None:
    """429 when this address or the fleet may not add another machine."""
    state = request.app.state
    wait = state.new_registrations.allow(
        state.backoff.client_ip(request), state.now().timestamp()
    )
    if wait is not None:
        return Response(status_code=429, headers={"Retry-After": str(math.ceil(wait))})
    unclaimed = await asyncio.to_thread(state.repo.count_unclaimed_machines)
    if unclaimed >= state.settings.max_unclaimed_machines:
        _log_cap_reached(state, unclaimed)
        return Response(
            status_code=429, headers={"Retry-After": str(CAP_RETRY_AFTER_SECONDS)}
        )
    return None


async def _register(request: Request, fp: str, body: RegisterMachine):
    return await asyncio.to_thread(
        request.app.state.repo.register_machine,
        fp,
        body.public_key_b64,
        body.label,
        request.app.state.now(),
    )


@router.post("/machines/register", response_model=RegisterResponse)
async def register(
    body: RegisterMachine,
    request: Request,
    _machine: Machine | None = Depends(require_machine),
) -> RegisterResponse | Response:
    fp = request.headers[HEADER_MACHINE]
    state = request.app.state
    if await asyncio.to_thread(state.repo.machine, fp) is not None:
        row = await _register(request, fp, body)  # known: never metered
    else:
        # New fingerprints are serialised so the meter and the cap are checked
        # and charged atomically; a burst cannot all pass the same count.
        async with state.register_lock:
            refusal = await _admit_new(request, fp)
            if refusal is not None:
                return refusal
            row = await _register(request, fp, body)
            state.new_registrations.record(
                state.backoff.client_ip(request), state.now().timestamp()
            )
    return RegisterResponse(
        fingerprint=fp,
        claimed=row.owner_id is not None,
        alerts_handled=await asyncio.to_thread(
            alerts_handled_for,
            request.app.state.repo,
            request.app.state.settings,
            row,
        ),
    )


@router.post("/machines/{fingerprint}/report", response_model=ReportResponse)
async def post_report(
    report: MachineReport,
    request: Request,
    machine: Machine = Depends(require_machine),
) -> ReportResponse | Response:
    state = request.app.state
    if (
        machine.owner_id is None
        and len(await request.body()) > state.settings.max_unclaimed_report_bytes
    ):
        return Response(status_code=413)
    saved = await asyncio.to_thread(state.store.save, machine.fingerprint, report)
    if saved:
        try:
            await _handle_alerts(request, machine, report)
        except Exception:
            logger.exception("Alert handling failed for {}", machine.fingerprint[:8])
    else:
        logger.debug(
            "Stale report ignored for alerts: {} seq={}",
            machine.fingerprint[:8],
            report.seq,
        )
    try:
        handled = await asyncio.to_thread(
            alerts_handled_for, state.repo, state.settings, machine
        )
    except Exception:
        logger.exception("Alert status failed for {}", machine.fingerprint[:8])
        handled = False
    return ReportResponse(alerts_handled=handled)


def _record_alerts(repo, fingerprint: str, report: MachineReport, now):
    previous = repo.alert_state(fingerprint)
    events = diff_reports(
        previous.faults if previous else (), report, now=now, fingerprint=fingerprint
    )
    if previous is not None and previous.offline_since is not None:
        events.append(
            AlertEvent(
                fingerprint=fingerprint,
                kind="online",
                at=now,
                key=None,
                code=None,
                severity=None,
                gate=None,
                description=None,
                sku=None,
                product=None,
            )
        )
    return repo.record_alert_pass(
        events,
        AlertState(
            fingerprint=fingerprint,
            faults=tuple(report.faults),
            last_report_at=now,
            report_interval_seconds=report.report_interval_seconds,
            offline_since=None,
        ),
    )


async def _handle_alerts(request: Request, machine: Machine, report: MachineReport):
    if machine.owner_id is None:
        return  # unclaimed machines never produce events
    state = request.app.state
    now = state.now()
    stored = await asyncio.to_thread(
        _record_alerts, state.repo, machine.fingerprint, report, now
    )
    state.alerts.schedule(machine.fingerprint, machine, stored)


@router.post("/machines/{fingerprint}/claim-code", status_code=204)
async def post_claim_code(
    notice: ClaimCodeNotice,
    request: Request,
    machine: Machine = Depends(require_machine),
):
    await asyncio.to_thread(
        request.app.state.repo.set_claim_code,
        machine.fingerprint,
        notice.claim_code_hash,
        notice.expires_at,
    )
    return Response(status_code=204)


@router.delete("/machines/{fingerprint}/claim-code", status_code=204)
async def delete_claim_code(
    request: Request, machine: Machine = Depends(require_machine)
):
    await asyncio.to_thread(
        request.app.state.repo.set_claim_code, machine.fingerprint, None, None
    )
    return Response(status_code=204)


@router.put("/ui-bundles/{digest}")
async def put_bundle(
    digest: str,
    request: Request,
    machine: Machine | None = Depends(require_machine),
):
    # Only claimed machines may store bundles, a few distinct digests each:
    # registration is open, so this bounds disk use per owned machine.
    if machine is None or machine.owner_id is None:
        return Response(status_code=401)
    repo = request.app.state.repo
    if request.app.state.registry.has(digest):
        return JSONResponse({"digest": digest}, status_code=200)  # nothing new to store
    now = request.app.state.now()
    if not await asyncio.to_thread(
        repo.can_upload_bundle,
        machine.fingerprint,
        digest,
        MAX_BUNDLES_PER_MACHINE,
        now - BUNDLE_WINDOW,
    ):
        return Response(status_code=409)
    # Size is capped by BodyLimitMiddleware as bytes arrive.
    data = await request.body()
    try:
        created = await asyncio.to_thread(
            request.app.state.registry.register, digest, data
        )
    except BundleError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    await asyncio.to_thread(repo.record_bundle_upload, machine.fingerprint, digest, now)
    return JSONResponse({"digest": digest}, status_code=201 if created else 200)
