"""Ingest API: machines push reports and UI bundles (cloud spec sections 3, 7)."""

from __future__ import annotations

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
    request.app.state.store.save(report)
    return Response(status_code=204)


@router.put("/ui-bundles/{digest}")
async def put_bundle(digest: str, request: Request):
    data = await request.body()
    if len(data) > MAX_UNCOMPRESSED_BYTES:
        return JSONResponse({"detail": "bundle too large"}, status_code=400)
    try:
        created = request.app.state.registry.register(digest, data)
    except BundleError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"digest": digest}, status_code=201 if created else 200)
