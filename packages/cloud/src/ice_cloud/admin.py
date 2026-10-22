"""Platform admin (identity spec 5.5): unbind machines, suspend accounts.

There is deliberately no route that sets an owner: ownership only ever passes
through a code minted at the machine.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger

from ice_cloud.auth import require_admin, require_htmx
from ice_cloud.store import delete_snapshots
from ice_cloud.store.repository import Account, Machine
from ice_cloud.viewer import RemoteViewer
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud.signing import parse_short_fingerprint, short_fingerprint
from ice_ui.levels import Level

LEVEL_ADMIN = Level(title="Admin", url="/admin")

router = APIRouter(prefix="/admin")


def _ctx(request: Request, account: Account, level: Level | None, **extra) -> dict:
    return {
        "request": request,
        "current_user": RemoteViewer(display_name=account.name),
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
        **extra,
    }


def _templates(request: Request):
    registry = request.app.state.registry
    return registry.resolve(registry.installed_digest).templates


async def _machine_or_404(request: Request, short: str) -> Machine:
    short16 = parse_short_fingerprint(short)
    machine = (
        await asyncio.to_thread(request.app.state.repo.resolve_short, short16)
        if short16
        else None
    )
    if machine is None:
        raise HTTPException(status_code=404)
    return machine


def _unbind_fragment(request: Request, short: str, confirming: bool) -> Response:
    base = f"/admin/machines/{short}/unbind"
    return _templates(request).TemplateResponse(
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


def _purge_fragment(request: Request, confirming: bool, **extra) -> Response:
    base = "/admin/purge-unclaimed"
    return _templates(request).TemplateResponse(
        request,
        "partials/admin_unclaimed_purge.html",
        {
            "label": "Purge all unclaimed",
            "confirm_label": "Confirm purge",
            "post_url": base,
            "confirm_url": f"{base}/confirm",
            "target": "#unclaimed-purge",
            "confirming": confirming,
            **extra,
        },
    )


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, account: Account = Depends(require_admin)):
    state = request.app.state
    now = state.now()
    machines = await asyncio.to_thread(state.repo.all_machines)
    accounts = await asyncio.to_thread(state.repo.all_accounts)
    by_id = {a.id: a for a in accounts}
    rows = []
    for m in machines:
        owner = by_id.get(m.owner_id) if m.owner_id else None
        rows.append(
            {
                "label": m.label,
                "short": short_fingerprint(m.fingerprint),
                "owner": owner.email if owner else None,
                "last_seen": m.last_seen_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "claim_open": bool(
                    m.claim_code_hash
                    and (
                        m.claim_code_expires_at is None or m.claim_code_expires_at > now
                    )
                ),
                "claimed": m.owner_id is not None,
            }
        )
    people = [
        {
            "id": a.id,
            "email": a.email,
            "name": a.name,
            "suspended": a.suspended_at is not None,
            "is_self": a.id == account.id,
        }
        for a in accounts
    ]
    return _templates(request).TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            account,
            LEVEL_ADMIN,
            machines=rows,
            unclaimed=await asyncio.to_thread(state.repo.count_unclaimed_machines),
            accounts=people,
            smtp_host=state.settings.smtp_host
            if state.settings.smtp_configured
            else None,
        ),
    )


@router.get(
    "/machines/{short}/unbind/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def unbind_confirm(request: Request, short: str, confirming: str | None = None):
    await _machine_or_404(request, short)
    return _unbind_fragment(request, short, confirming != "false")


@router.post(
    "/machines/{short}/unbind",
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def unbind(request: Request, short: str):
    machine = await _machine_or_404(request, short)
    await asyncio.to_thread(request.app.state.repo.unbind_machine, machine.fingerprint)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.get(
    "/purge-unclaimed/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def purge_unclaimed_confirm(request: Request, confirming: str | None = None):
    return _purge_fragment(request, confirming != "false")


@router.post(
    "/purge-unclaimed",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
async def purge_unclaimed(request: Request):
    state = request.app.state
    purged = await asyncio.to_thread(state.repo.purge_all_unclaimed_machines)
    await delete_snapshots(state.store, purged)
    logger.warning("Admin purged {} unclaimed machines", len(purged))
    return _purge_fragment(
        request,
        False,
        oob=True,
        unclaimed=await asyncio.to_thread(state.repo.count_unclaimed_machines),
        purged=len(purged),
    )


async def _set_suspended(request: Request, account_id: str, suspend: bool) -> Response:
    state = request.app.state
    target = await asyncio.to_thread(state.repo.account, account_id)
    if target is None:
        raise HTTPException(status_code=404)
    await asyncio.to_thread(
        state.repo.set_suspended, account_id, state.now() if suspend else None
    )
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/accounts/{account_id}/suspend", dependencies=[Depends(require_htmx)])
async def suspend(
    request: Request, account_id: str, admin: Account = Depends(require_admin)
):
    if account_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot suspend yourself")
    return await _set_suspended(request, account_id, True)


@router.post("/accounts/{account_id}/unsuspend", dependencies=[Depends(require_htmx)])
async def unsuspend(
    request: Request, account_id: str, _admin: Account = Depends(require_admin)
):
    return await _set_suspended(request, account_id, False)


def _test_email_fragment(text: str, ok: bool) -> HTMLResponse:
    tone = "text-gray-600" if ok else "text-red-600"
    return HTMLResponse(f'<span class="text-xs {tone}">{escape(text)}</span>')


@router.post("/test-email", dependencies=[Depends(require_htmx)])
async def test_email(request: Request, admin: Account = Depends(require_admin)):
    state = request.app.state
    msg = EmailMessage()
    msg["From"] = state.settings.smtp_from or "ice-colder@localhost"
    msg["To"] = admin.email
    msg["Subject"] = "[ice-colder] test email"
    msg.set_content("This test email was sent from the ice-colder cloud admin page.")
    try:
        await state.mailer.send(msg)
    except Exception as exc:
        logger.warning("Admin test email to {} failed: {}", admin.email, exc)
        return _test_email_fragment(f"Failed: {exc}", False)
    return _test_email_fragment(f"Sent to {admin.email}", True)
