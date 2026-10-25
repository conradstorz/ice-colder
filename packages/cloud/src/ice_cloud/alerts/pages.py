"""The alert history page (alerts spec 5): the last 200 events of a machine
with each recipient's delivery state. Owners and techs may read it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_cloud.auth import require_machine_access
from ice_cloud.people import recipients_context
from ice_cloud.portal import LEVEL_FLEET, page_context, resolve_ui
from ice_contracts.cloud.signing import short_fingerprint
from ice_ui.levels import Level

router = APIRouter()

HISTORY_LIMIT = 200


def _utc(at) -> datetime:
    return at.astimezone(UTC)


def _delivery_line(status: str, sent_at, error: str | None, *, detail: bool) -> str:
    if status == "sent" and sent_at is not None:
        return f"sent {_utc(sent_at):%H:%M} UTC"
    if status == "failed":
        return f"failed: {error or 'unknown error'}" if detail else "failed"
    return status


@router.get("/machines/{short}/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, access=Depends(require_machine_access())):
    """Owners see every delivery in full. A technician sees their own
    lines in full and the others only as "owner" / "technician" with a status:
    no email addresses and no SMTP error text."""
    account, machine, role = access
    repo = request.app.state.repo
    events = await asyncio.to_thread(
        repo.alert_events, machine.fingerprint, limit=HISTORY_LIMIT
    )
    by_event = await asyncio.to_thread(
        repo.deliveries_for_events, machine.fingerprint, [e.id for e in events]
    )
    privileged = role == "owner"
    names: dict[str, str] = {}
    rows = []
    for e in events:
        lines = []
        for d in by_event.get(e.id, []):
            mine = d.account_id == account.id
            if privileged:
                if d.account_id not in names:
                    who = await asyncio.to_thread(repo.account, d.account_id)
                    names[d.account_id] = who.email if who else d.account_id
                label = names[d.account_id]
            elif mine:
                label = account.email
            else:
                label = "owner" if d.account_id == machine.owner_id else "technician"
            lines.append(
                {
                    "who": label,
                    "text": _delivery_line(
                        d.status, d.sent_at, d.error, detail=privileged or mine
                    ),
                }
            )
        rows.append(
            {
                "kind": e.kind,
                "at": f"{_utc(e.at):%Y-%m-%d %H:%M} UTC",
                "code": e.code or "",
                "description": e.description or e.key or "",
                "product": e.product or "",
                "deliveries": lines,
            }
        )
    recipients = (
        await recipients_context(request, machine, account, role)
        if role in ("owner", "tech")
        else None
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
            recipients=recipients,
        ),
    )
