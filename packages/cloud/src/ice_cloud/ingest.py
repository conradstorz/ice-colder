"""Ingest API: machines push reports and UI bundles (cloud spec sections 3, 7)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ice_cloud.machine_auth import require_machine
from ice_cloud.store.repository import Machine
from ice_contracts.cloud.identity import (
    ClaimCodeNotice,
    RegisterMachine,
    RegisterResponse,
)
from ice_contracts.cloud.report import MachineReport
from ice_contracts.cloud.signing import HEADER_MACHINE
from ice_ui.bundle import MAX_UNCOMPRESSED_BYTES, BundleError

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
    return RegisterResponse(fingerprint=fp, claimed=row.owner_id is not None)


@router.post("/machines/{fingerprint}/report", status_code=204)
async def post_report(
    report: MachineReport,
    request: Request,
    machine: Machine = Depends(require_machine),
):
    await asyncio.to_thread(request.app.state.store.save, machine.fingerprint, report)
    return Response(status_code=204)


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


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": "bundle too large"}, status_code=400)


@router.put("/ui-bundles/{digest}")
async def put_bundle(
    digest: str,
    request: Request,
    _machine: Machine | None = Depends(require_machine),
):
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > MAX_UNCOMPRESSED_BYTES:
            return _too_large()
    # Read in chunks so a body without (or lying about) Content-Length is
    # still cut off at the limit instead of being buffered whole.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UNCOMPRESSED_BYTES:
            return _too_large()
        chunks.append(chunk)
    try:
        created = await asyncio.to_thread(
            request.app.state.registry.register, digest, b"".join(chunks)
        )
    except BundleError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"digest": digest}, status_code=201 if created else 200)
