"""Fleet alerts level: the newest alert events across every machine."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.admin.common import LEVEL_ADMIN_ALERTS, admin_context, templates
from ice_cloud.alerts.format import event_row
from ice_cloud.auth import find_machine, require_admin
from ice_cloud.store.repository import Account
from ice_contracts.cloud.signing import short_fingerprint

router = APIRouter(prefix="/admin")

ALERTS_LIMIT = 100


@router.get("/alerts", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    machine: str | None = None,
    account: Account = Depends(require_admin),
):
    repo = request.app.state.repo
    selected = None
    if machine:
        selected = await find_machine(repo, machine)
    pairs = await asyncio.to_thread(
        repo.recent_alert_events,
        limit=ALERTS_LIMIT,
        fingerprint=selected.fingerprint if selected else None,
    )
    events = []
    for event, m in pairs:
        short = short_fingerprint(m.fingerprint)
        events.append(
            {
                **event_row(event),
                "label": m.label,
                "short": short,
            }
        )
    return templates(request).TemplateResponse(
        request,
        "admin_alerts.html",
        admin_context(
            request,
            account,
            LEVEL_ADMIN_ALERTS,
            events=events,
            filtered=(
                {
                    "label": selected.label,
                    "short": short_fingerprint(selected.fingerprint),
                }
                if selected
                else None
            ),
        ),
    )
