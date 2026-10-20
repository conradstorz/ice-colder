"""Ingest API: machines push reports and UI bundles (cloud spec sections 3, 7)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger

from ice_cloud.alerts.engine import AlertEvent, diff_reports
from ice_cloud.machine_auth import require_machine
from ice_cloud.store.repository import AlertState, Machine
from ice_contracts.cloud.identity import (
    ClaimCodeNotice,
    RegisterMachine,
    RegisterResponse,
)
from ice_contracts.cloud.report import MachineReport
from ice_contracts.cloud.signing import HEADER_MACHINE
from ice_ui.bundle import BundleError

MAX_BUNDLES_PER_MACHINE = 4  # distinct digests per machine within BUNDLE_WINDOW
BUNDLE_WINDOW = timedelta(days=30)

router = APIRouter(prefix="/api/v1")


@router.post("/machines/register")
async def register(
    body: RegisterMachine,
    request: Request,
    _machine: Machine | None = Depends(require_machine),
) -> RegisterResponse:
    fp = request.headers[HEADER_MACHINE]
    row = await asyncio.to_thread(
        request.app.state.repo.register_machine,
        fp,
        body.public_key_b64,
        body.label,
        request.app.state.now(),
    )
    return RegisterResponse(
        fingerprint=fp,
        claimed=row.owner_id is not None,
        alerts_handled=row.owner_id is not None
        and request.app.state.settings.smtp_configured,
    )


@router.post("/machines/{fingerprint}/report", status_code=204)
async def post_report(
    report: MachineReport,
    request: Request,
    machine: Machine = Depends(require_machine),
):
    state = request.app.state
    await asyncio.to_thread(state.store.save, machine.fingerprint, report)
    try:
        await _handle_alerts(request, machine, report)
    except Exception:
        logger.exception("Alert handling failed for {}", machine.fingerprint[:8])
    return Response(status_code=204)


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
    stored = repo.record_alert_events(events)
    repo.put_alert_state(
        AlertState(
            fingerprint=fingerprint,
            faults=tuple(report.faults),
            last_report_at=now,
            report_interval_seconds=report.report_interval_seconds,
            offline_since=None,
        )
    )
    return stored


async def _handle_alerts(request: Request, machine: Machine, report: MachineReport):
    state = request.app.state
    now = state.now()
    stored = await asyncio.to_thread(
        _record_alerts, state.repo, machine.fingerprint, report, now
    )
    await state.alerts.dispatch(machine.fingerprint, machine, stored)
    await state.alerts.flush(state.now())


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
