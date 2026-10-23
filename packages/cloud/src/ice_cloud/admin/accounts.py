"""Accounts level: the people list, suspend/unsuspend, the test email."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger

from ice_cloud.admin.common import LEVEL_ADMIN_ACCOUNTS, admin_context, templates
from ice_cloud.auth import require_admin, require_htmx
from ice_cloud.store.repository import Account

router = APIRouter(prefix="/admin")


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, account: Account = Depends(require_admin)):
    state = request.app.state
    accounts = await asyncio.to_thread(state.repo.all_accounts)
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
    return templates(request).TemplateResponse(
        request,
        "admin_accounts.html",
        admin_context(
            request,
            account,
            LEVEL_ADMIN_ACCOUNTS,
            accounts=people,
            smtp_host=state.settings.smtp_host
            if state.settings.smtp_configured
            else None,
        ),
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
