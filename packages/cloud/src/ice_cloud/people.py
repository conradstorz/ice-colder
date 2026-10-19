"""People: the owner's memberships and invitations, and invitation acceptance
(identity spec 3 and 6.3). Invitation emails are out of scope; the owner is
shown a link to pass on."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ice_cloud.auth import require_htmx, require_machine_access, require_signed_in
from ice_cloud.portal import LEVEL_FLEET, _context, _resolve
from ice_cloud.store.repository import Account, Invitation, Machine
from ice_contracts.cloud.signing import short_fingerprint
from ice_ui.levels import Level

router = APIRouter()

_OWNER = require_machine_access(owner_only=True)


def normalize_email(raw: str) -> str | None:
    """Lower-cased address, or None unless it has one ``@`` and no whitespace."""
    email = raw.strip().lower()
    if not email or any(c.isspace() for c in email) or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    return email if local and domain else None


def _base_url(request: Request) -> str:
    public = request.app.state.settings.public_url
    return (public or str(request.base_url)).rstrip("/")


def _revoke_ctx(short: str, account_id: str, confirming: bool) -> dict:
    return {
        "label": "Revoke",
        "confirm_label": "Confirm revoke",
        "post_url": f"/machines/{short}/people/{account_id}/revoke",
        "confirm_url": f"/machines/{short}/people/{account_id}/revoke/confirm",
        "target": f"#revoke-{account_id}",
        "confirming": confirming,
    }


async def _page(
    request: Request,
    account: Account,
    machine: Machine,
    *,
    error: str | None = None,
    new_link: str | None = None,
    status_code: int = 200,
) -> Response:
    state = request.app.state
    short = short_fingerprint(machine.fingerprint)
    members = await asyncio.to_thread(state.repo.memberships, machine.fingerprint)
    pending = await asyncio.to_thread(
        state.repo.invitations_by, account.id, machine.fingerprint, state.now()
    )
    base = _base_url(request)
    people = [
        {"name": a.name, "email": a.email, **_revoke_ctx(short, a.id, False)}
        for m, a in members
        if m.role == "tech"
    ]
    invites = [
        {
            "token": i.token,
            "email": i.email,
            "fleet": i.scope == "fleet",
            "link": f"{base}/invite/{i.token}",
        }
        for i in pending
    ]
    ui = _resolve(request, None)
    level = Level.child(LEVEL_FLEET, "People", f"/machines/{short}/people")
    return ui.templates.TemplateResponse(
        request,
        "people.html",
        _context(
            request,
            account,
            ui,
            level,
            short=short,
            machine_label=machine.label,
            people=people,
            invites=invites,
            error=error,
            new_link=new_link,
        ),
        status_code=status_code,
    )


async def _tech_member(request: Request, machine: Machine, account_id: str) -> None:
    members = await asyncio.to_thread(
        request.app.state.repo.memberships, machine.fingerprint
    )
    if not any(m.account_id == account_id and m.role == "tech" for m, _ in members):
        raise HTTPException(status_code=404)


@router.get("/machines/{short}/people", response_class=HTMLResponse)
async def people_page(request: Request, access=Depends(_OWNER)):
    account, machine, _role = access
    return await _page(request, account, machine)


@router.get(
    "/machines/{short}/people/{account_id}/revoke/confirm", response_class=HTMLResponse
)
async def revoke_confirm(
    request: Request,
    account_id: str,
    confirming: str | None = None,
    access=Depends(_OWNER),
):
    _account, machine, _role = access
    await _tech_member(request, machine, account_id)
    ui = _resolve(request, None)
    short = short_fingerprint(machine.fingerprint)
    return ui.templates.TemplateResponse(
        request,
        "partials/people_revoke.html",
        {"request": request, **_revoke_ctx(short, account_id, confirming != "false")},
    )


@router.post(
    "/machines/{short}/people/{account_id}/revoke",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def revoke(request: Request, account_id: str, access=Depends(_OWNER)):
    _account, machine, _role = access
    await _tech_member(request, machine, account_id)
    await asyncio.to_thread(
        request.app.state.repo.revoke, account_id, machine.fingerprint
    )
    return HTMLResponse(
        f'<span id="revoke-{account_id}" class="text-xs text-gray-500">Revoked</span>'
    )


@router.post(
    "/machines/{short}/people/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def invite(
    request: Request,
    email: str = Form(""),
    scope: str = Form("machine"),
    access=Depends(_OWNER),
):
    account, machine, _role = access
    address = normalize_email(email)
    if address is None or scope not in ("machine", "fleet"):
        return await _page(
            request,
            account,
            machine,
            error="Enter a valid email address (for example name@example.com).",
            status_code=400,
        )
    state = request.app.state
    stored = "fleet" if scope == "fleet" else (machine.fingerprint,)
    created = await asyncio.to_thread(
        state.repo.create_invitation, account.id, address, stored, state.now()
    )
    return await _page(
        request,
        account,
        machine,
        new_link=f"{_base_url(request)}/invite/{created.token}",
    )


@router.post(
    "/machines/{short}/people/invitations/{token}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def cancel(request: Request, token: str, access=Depends(_OWNER)):
    account, _machine, _role = access
    repo = request.app.state.repo
    found = await asyncio.to_thread(repo.invitation, token)
    if found is None or found.inviter_id != account.id or found.accepted_by:
        raise HTTPException(status_code=404)
    await asyncio.to_thread(repo.cancel_invitation, token)
    return HTMLResponse("")


async def _open_invitation(request: Request, token: str) -> Invitation:
    state = request.app.state
    found = await asyncio.to_thread(state.repo.invitation, token)
    if found is None or found.accepted_by or found.expires_at <= state.now():
        raise HTTPException(status_code=404)
    return found


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(
    request: Request, token: str, account: Account = Depends(require_signed_in)
):
    found = await _open_invitation(request, token)
    inviter = await asyncio.to_thread(request.app.state.repo.account, found.inviter_id)
    ui = _resolve(request, None)
    return ui.templates.TemplateResponse(
        request,
        "invite.html",
        _context(
            request,
            account,
            ui,
            Level.child(LEVEL_FLEET, "Invitation", f"/invite/{token}"),
            token=token,
            inviter_name=inviter.name if inviter else "An owner",
            fleet=found.scope == "fleet",
            machine_count=0 if found.scope == "fleet" else len(found.scope),
            matches=account.email.lower() == found.email.lower(),
            invited_email=found.email,
        ),
    )


@router.post("/invite/{token}", dependencies=[Depends(require_htmx)])
async def accept(
    request: Request, token: str, account: Account = Depends(require_signed_in)
):
    found = await _open_invitation(request, token)
    if account.email.lower() != found.email.lower():
        raise HTTPException(
            status_code=403, detail="Invitation is for a different address"
        )
    state = request.app.state
    granted = await asyncio.to_thread(
        state.repo.accept_invitation, token, account.id, state.now()
    )
    if not granted:
        raise HTTPException(status_code=404)
    return RedirectResponse("/fleet", status_code=303)
