"""The alert history page (alerts spec 5): the last 200 events of a machine
with each recipient's delivery state. Owners and techs may read it."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.auth import require_machine_access
from ice_cloud.portal import LEVEL_FLEET, page_context, resolve_ui
from ice_contracts.cloud.signing import short_fingerprint
from ice_ui.levels import Level

router = APIRouter()

HISTORY_LIMIT = 200


def _delivery_line(status: str, sent_at, error: str | None) -> str:
    if status == "sent" and sent_at is not None:
        return f"sent {sent_at.astimezone().strftime('%H:%M')}"
    if status == "failed":
        return f"failed: {error or 'unknown error'}"
    return status


@router.get("/machines/{short}/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, access=Depends(require_machine_access())):
    account, machine, _role = access
    repo = request.app.state.repo
    events = await asyncio.to_thread(
        repo.alert_events, machine.fingerprint, limit=HISTORY_LIMIT
    )
    by_event = await asyncio.to_thread(
        repo.deliveries_for_events, [e.id for e in events]
    )
    names: dict[str, str] = {}
    rows = []
    for e in events:
        lines = []
        for d in by_event.get(e.id, []):
            if d.account_id not in names:
                who = await asyncio.to_thread(repo.account, d.account_id)
                names[d.account_id] = who.email if who else d.account_id
            lines.append(
                {
                    "who": names[d.account_id],
                    "text": _delivery_line(d.status, d.sent_at, d.error),
                }
            )
        rows.append(
            {
                "kind": e.kind,
                "at": e.at.astimezone().strftime("%Y-%m-%d %H:%M"),
                "code": e.code or "",
                "description": e.description or e.key or "",
                "product": e.product or "",
                "deliveries": lines,
            }
        )
    short = short_fingerprint(machine.fingerprint)
    ui = resolve_ui(request, None)
    level = Level.child(LEVEL_FLEET, "Alerts", f"/machines/{short}/alerts")
    return ui.templates.TemplateResponse(
        request,
        "alerts.html",
        page_context(
            request,
            account,
            ui,
            level,
            short=short,
            machine_label=machine.label,
            events=rows,
        ),
    )
