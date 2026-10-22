"""Machines level: the filterable fleet table and the two-tap unbind."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ice_cloud.admin.common import (
    LEVEL_ADMIN_MACHINES,
    admin_context,
    load_fleet_view,
    machine_or_404,
    templates,
)
from ice_cloud.auth import require_admin, require_htmx
from ice_cloud.fleet_view import MachineStatus, filter_rows
from ice_cloud.store.repository import Account
from ice_ui.levels import Level

router = APIRouter(prefix="/admin")

# (label, query) in display order; the first is the unfiltered list.
_PILLS: tuple[tuple[str, dict[str, str]], ...] = (
    ("All", {}),
    ("Online", {"status": "online"}),
    ("Silent", {"status": "silent"}),
    ("Never reported", {"status": "never_reported"}),
    ("Unclaimed", {"claimed": "no"}),
    ("Faulted", {"faulted": "yes"}),
    ("Payment blocked", {"payment": "blocked"}),
)


def _title(query: dict[str, str], owner_email: str | None) -> str:
    """Active filters in pill order, joined; the owner's email leads."""
    parts: list[str] = []
    if "owner" in query:
        parts.append(owner_email or query["owner"])
    if "claimed" in query:
        parts.append("Claimed" if query["claimed"] == "yes" else "Unclaimed")
    if "status" in query:
        parts.append(query["status"].replace("_", " ").capitalize())
    if "faulted" in query:
        parts.append("Faulted")
    if "payment" in query:
        parts.append("Payment blocked")
    return " · ".join(parts)


def _url(query: dict[str, str]) -> str:
    base = LEVEL_ADMIN_MACHINES.url
    return f"{base}?{urlencode(query)}" if query else base


def _unbind_fragment(request: Request, short: str, confirming: bool) -> Response:
    base = f"/admin/machines/{short}/unbind"
    return templates(request).TemplateResponse(
        request,
        "partials/admin_unbind.html",
        {
            "label": "Unbind",
            "confirm_label": "Confirm unbind",
            "post_url": base,
            "confirm_url": f"{base}/confirm",
            "target": f"#unbind-{short}",
            "confirming": confirming,
        },
    )


@router.get("/machines", response_class=HTMLResponse)
async def machines_page(
    request: Request,
    status: str | None = None,
    claimed: str | None = None,
    faulted: str | None = None,
    payment: str | None = None,
    owner: str | None = None,
    account: Account = Depends(require_admin),
):
    # Unknown values are ignored, never an error.
    query: dict[str, str] = {}
    if status in {s.value for s in MachineStatus}:
        query["status"] = status
    if claimed in {"yes", "no"}:
        query["claimed"] = claimed
    if faulted == "yes":
        query["faulted"] = "yes"
    if payment == "blocked":
        query["payment"] = "blocked"
    if owner:
        query["owner"] = owner

    view = await load_fleet_view(request)
    rows = filter_rows(
        view.rows,
        status=MachineStatus(query["status"]) if "status" in query else None,
        claimed=(query["claimed"] == "yes") if "claimed" in query else None,
        faulted=True if "faulted" in query else None,
        payment="blocked" if "payment" in query else None,
        owner=query.get("owner"),
    )

    owner_email = None
    if "owner" in query:
        owner_email = next(
            (r.owner_email for r in view.rows if r.owner_id == owner), None
        )
        if owner_email is None:
            found = await asyncio.to_thread(request.app.state.repo.account, owner)
            owner_email = found.email if found else owner

    filters = [
        {"label": label, "url": _url(q), "active": q == query} for label, q in _PILLS
    ]
    level = LEVEL_ADMIN_MACHINES
    heading = "Machines"
    if query:
        title = _title(query, owner_email)
        level = Level.child(LEVEL_ADMIN_MACHINES, title, _url(query))
        heading = f"Machines — {title}"
    return templates(request).TemplateResponse(
        request,
        "admin_machines.html",
        admin_context(
            request,
            account,
            level,
            rows=rows,
            filters=filters,
            heading=heading,
            count=len(rows),
        ),
    )


@router.get(
    "/machines/{short}/unbind/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def unbind_confirm(request: Request, short: str, confirming: str | None = None):
    await machine_or_404(request, short)
    return _unbind_fragment(request, short, confirming != "false")


@router.post(
    "/machines/{short}/unbind",
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def unbind(request: Request, short: str):
    machine = await machine_or_404(request, short)
    await asyncio.to_thread(request.app.state.repo.unbind_machine, machine.fingerprint)
    return Response(status_code=200, headers={"HX-Refresh": "true"})
