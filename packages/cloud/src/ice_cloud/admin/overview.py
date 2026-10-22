"""The admin landing page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.admin.common import LEVEL_ADMIN, admin_context, templates
from ice_cloud.auth import require_admin
from ice_cloud.store.repository import Account

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, account: Account = Depends(require_admin)):
    return templates(request).TemplateResponse(
        request, "admin.html", admin_context(request, account, LEVEL_ADMIN)
    )
