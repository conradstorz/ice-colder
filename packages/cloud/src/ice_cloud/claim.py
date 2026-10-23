"""Claiming a machine with the code minted at it (identity spec 5.4).

Order is fixed: back-off (429), resolve + expiry (403), hash check (403), then
one atomic repository claim. Every failure mode shows the same 403 text so the
form cannot be used to discover machines.
"""

from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ice_cloud.auth import require_htmx, require_signed_in
from ice_cloud.portal import COOKIE, LEVEL_FLEET
from ice_cloud.store.repository import Account
from ice_cloud.viewer import RemoteViewer
from ice_common.secrets import hash_secret, verify_secret
from ice_contracts.access import REMOTE_VIEWER_PERMISSIONS
from ice_contracts.cloud.signing import parse_short_fingerprint
from ice_ui.levels import Level

LEVEL_CLAIM = Level(title="Claim a machine", url="/claim", parent=LEVEL_FLEET)
NO_CLAIM = "No claim is open for this machine, or the code is wrong."

DUMMY_HASH = hash_secret("00000000")

router = APIRouter()


async def _scrypt(state, fn, *args):
    """Run a scrypt call on the dedicated executor, off the default pool.

    Each call costs ~16 MB and tens of ms; sharing the default executor with
    SQLite and ingest would let claim attempts starve them on a small host.
    """
    return await asyncio.get_running_loop().run_in_executor(
        state.scrypt_executor, fn, *args
    )


def _page(
    request: Request,
    account: Account,
    *,
    error: str | None = None,
    fingerprint: str = "",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    registry = request.app.state.registry
    ui = registry.resolve(registry.installed_digest)
    return ui.templates.TemplateResponse(
        request,
        "claim.html",
        {
            "request": request,
            "current_user": RemoteViewer(display_name=account.name),
            "perms": REMOTE_VIEWER_PERMISSIONS,
            "level": LEVEL_CLAIM,
            "error": error,
            "fingerprint": fingerprint,
        },
        status_code=status_code,
        headers=headers,
    )


@router.get("/claim", response_class=HTMLResponse)
async def claim_form(request: Request, account: Account = Depends(require_signed_in)):
    return _page(request, account)


@router.post("/claim", dependencies=[Depends(require_htmx)])
async def claim_submit(
    request: Request,
    fingerprint: str = Form(""),
    code: str = Form(""),
    account: Account = Depends(require_signed_in),
):
    state = request.app.state
    backoff = state.backoff
    short = parse_short_fingerprint(fingerprint)
    subject = short or "unknown"
    client = request.client.host if request.client else "unknown"

    remaining = backoff.check("claim", subject, client, trusted=False)
    if remaining is not None:
        wait = int(remaining) + 1
        return _page(
            request,
            account,
            error=f"Too many attempts. Try again in {wait} seconds.",
            fingerprint=fingerprint,
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    async def fail(*, burn: bool = False, probing_live_code: bool = False) -> Response:
        if burn:
            await _scrypt(state, verify_secret, code.strip(), DUMMY_HASH)
        # Only a wrong code against a resolved, live claim feeds the per-machine
        # budget; anything else is throttled per client alone, so a signed-in
        # account cannot lock a machine (or the shared "unknown" bucket) out.
        backoff.record_failure("claim", subject, client, trusted=not probing_live_code)
        return _page(
            request,
            account,
            error=NO_CLAIM,
            fingerprint=fingerprint,
            status_code=403,
        )

    machine = (
        await asyncio.to_thread(state.repo.resolve_short, short) if short else None
    )
    now = state.now()
    if (
        machine is None
        or machine.claim_code_hash is None
        or (
            machine.claim_code_expires_at is not None
            and machine.claim_code_expires_at <= now
        )
    ):
        return await fail(burn=True)
    if not await _scrypt(state, verify_secret, code.strip(), machine.claim_code_hash):
        return await fail(probing_live_code=True)
    if not await asyncio.to_thread(
        partial(
            state.repo.claim_machine,
            machine.fingerprint,
            account.id,
            now,
            expected_hash=machine.claim_code_hash,
        )
    ):
        return await fail(probing_live_code=True)

    backoff.record_success("claim", subject, client)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        machine.fingerprint,
        httponly=True,
        samesite="lax",
        secure=not state.settings.insecure_cookies,
    )
    return response
