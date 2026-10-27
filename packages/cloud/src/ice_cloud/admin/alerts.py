"""Fleet alerts level: the newest alert events across every machine."""

from __future__ import annotations

import asyncio
from datetime import UTC

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.admin.common import LEVEL_ADMIN_ALERTS, admin_context, templates
from ice_cloud.auth import require_admin
from ice_cloud.store.repository import Account
from ice_contracts.cloud.signing import parse_short_fingerprint, short_fingerprint

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
        short16 = parse_short_fingerprint(machine)
        if short16:
            selected = await asyncio.to_thread(repo.resolve_short, short16)
    pairs = await asyncio.to_thread(
        repo.recent_alert_events,
        limit=ALERTS_LIMIT,
        fingerprint=selected.fingerprint if selected else None,
    )
    events = []
    for event, machine in pairs:
        short = short_fingerprint(machine.fingerprint)
        events.append(
            {
                "at": f"{event.at.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
                "kind": event.kind,
                "code": event.code or "",
                "description": event.description or event.key or "",
                "product": event.product or "",
                "label": machine.label,
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
