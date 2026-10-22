"""Owners level: one row per account that owns at least one machine."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.admin.common import (
    LEVEL_ADMIN_OWNERS,
    admin_context,
    load_fleet_view,
    templates,
)
from ice_cloud.auth import require_admin
from ice_cloud.store.repository import Account

router = APIRouter(prefix="/admin")


@router.get("/owners", response_class=HTMLResponse)
async def owners_page(request: Request, account: Account = Depends(require_admin)):
    view = await load_fleet_view(request)
    return templates(request).TemplateResponse(
        request,
        "admin_owners.html",
        admin_context(
            request,
            account,
            LEVEL_ADMIN_OWNERS,
            owners=view.owners,
            idle_accounts=view.account_count - len(view.owners),
        ),
    )
