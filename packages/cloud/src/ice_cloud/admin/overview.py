"""The admin landing page: the fleet overview and its polled fragment."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.admin.common import (
    LEVEL_ADMIN,
    admin_context,
    load_fleet_view,
    templates,
)
from ice_cloud.auth import require_admin
from ice_cloud.fleet_view import FleetView
from ice_cloud.store.repository import Account

router = APIRouter(prefix="/admin")

OWNERS_PANEL_SIZE = 8


def _overview_context(
    request: Request, account: Account, view: FleetView, **extra
) -> dict:
    return admin_context(
        request,
        account,
        LEVEL_ADMIN,
        view=view,
        computed_at=request.app.state.now().strftime("%H:%M:%S UTC"),
        owners_panel=view.owners[:OWNERS_PANEL_SIZE],
        machine_count=len(view.rows),
        **extra,
    )


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, account: Account = Depends(require_admin)):
    view = await load_fleet_view(request)
    return templates(request).TemplateResponse(
        request, "admin.html", _overview_context(request, account, view)
    )


@router.get("/overview", response_class=HTMLResponse)
async def admin_overview(request: Request, account: Account = Depends(require_admin)):
    view = await load_fleet_view(request)
    return templates(request).TemplateResponse(
        request,
        "partials/admin_overview_frame.html",
        _overview_context(request, account, view),
    )
