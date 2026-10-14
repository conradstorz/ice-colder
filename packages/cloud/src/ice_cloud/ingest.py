"""Ingest API: machines push reports and UI bundles (cloud spec sections 3, 7)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ice_cloud.store import MACHINE_ID_RE
from ice_contracts.cloud.report import MachineReport
from ice_ui.bundle import MAX_UNCOMPRESSED_BYTES, BundleError

router = APIRouter(prefix="/api/v1")


@router.post("/machines/{machine_id}/report", status_code=204)
async def post_report(machine_id: str, report: MachineReport, request: Request):
    if not MACHINE_ID_RE.fullmatch(machine_id) or report.machine_id != machine_id:
        raise HTTPException(
            status_code=422,
            detail="machine_id in the body does not match the URL",
        )
    await asyncio.to_thread(request.app.state.store.save, report)
    return Response(status_code=204)


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": "bundle too large"}, status_code=400)


@router.put("/ui-bundles/{digest}")
async def put_bundle(digest: str, request: Request):
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
