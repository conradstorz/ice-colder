"""Setup, setup-code and post-transfer review routes."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ice_edge.access.access import (
    OwnerExistsError,
    Permission,
    Role,
)
from ice_edge.access.auth_policy import pin_problem
from ice_edge.notify.mailer import send_email
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.templating import templates

router = APIRouter()


def _setup_page(
    request: Request,
    *,
    error: str | None = None,
    form: dict | None = None,
    status_code: int = 200,
    headers: dict | None = None,
):
    return templates.TemplateResponse(
        request,
        "setup.html",
        context.template_context(
            request,
            error=error,
            form=form or {"name": "", "email": ""},
            transfer=context.access_store.pending_transfer is not None,
        ),
        status_code=status_code,
        headers=headers or {},
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if context.access_store is None:
        raise HTTPException(status_code=503, detail="Access store not loaded")
    context.ensure_setup_mode()
    # Nothing to do here once an owner exists and no transfer is live —
    # send the visitor on to the normal login/dashboard flow instead of
    # showing a wizard with no code that would accept.
    if (
        not context.access_store.setup_mode
        and context.access_store.pending_transfer is None
    ):
        return RedirectResponse("/", status_code=303)
    return _setup_page(request)


@router.post(
    "/setup",
    response_class=HTMLResponse,
    dependencies=[Depends(context.require_htmx)],
)
async def setup_submit(
    request: Request,
    setup_code: str = Form(...),
    name: str = Form(...),
    email: str = Form(""),
    pin: str = Form(...),
    pin_confirm: str = Form(...),
    shared_device: str | None = Form(None),
):
    if context.access_store is None:
        raise HTTPException(status_code=503, detail="Access store not loaded")

    # Task 19 completes ownership transfer through this same handler:
    # while a transfer is pending, the code being checked is the
    # transfer code, not the setup code, and back-off is tracked
    # separately so a stranger guessing transfer codes can't also burn
    # down the setup code's budget or vice versa.
    in_transfer = context.access_store.pending_transfer is not None
    kind = "transfer" if in_transfer else "setup"
    form = {"name": name, "email": email}
    client = web_auth.client_key(request)

    remaining = web_auth.backoff.check(kind, kind, client)
    if remaining is not None:
        return _setup_page(
            request,
            error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
            form=form,
            status_code=429,
            headers={"Retry-After": str(int(remaining) + 1)},
        )

    code_ok = (
        context.access_store.verify_transfer_code(setup_code)
        if in_transfer
        else context.access_store.verify_setup_code(setup_code)
    )
    if not code_ok:
        web_auth.backoff.record_failure(kind, kind, client)
        return _setup_page(request, error="That code was not accepted.", form=form)
    web_auth.backoff.record_success(kind, kind, client)

    # A wrong code above is the one thing worth slowing a stranger down
    # for; a mismatched confirmation or a weak PIN is the owner's own
    # typo at the machine, so neither touches back-off (spec's ordering).
    if pin != pin_confirm:
        return _setup_page(request, error="PINs do not match.", form=form)

    problem = pin_problem(pin)
    if problem:
        return _setup_page(request, error=problem, form=form)

    shared = shared_device is not None
    try:
        if in_transfer:
            owner = context.access_store.complete_transfer(name, email or None, pin)
        else:
            owner = context.access_store.create_user(
                name, email or None, Role.owner, pin
            )
    except OwnerExistsError:
        # Two racing step-1 submissions: the store enforces one owner
        # and refuses the second, so this must read as an ordinary
        # error, never a 500 (spec §3.1).
        return _setup_page(
            request, error="This machine already has an owner.", form=form
        )

    device, token = context.access_store.create_device(
        f"{name}'s device", shared=shared
    )
    context.access_store.trust_device(device.id, owner.id)
    context.access_store.record_login(owner.id)
    session_id = context.access_store.create_session(owner.id, device.id)

    # A completed transfer still has the retained users to walk (spec
    # §3.3 step 3) before the codes step; ordinary first-owner setup
    # goes straight to the codes step as before.
    next_step = "/setup/review" if in_transfer else "/setup/codes"
    resp = HTMLResponse("", headers={"HX-Redirect": next_step})
    web_auth.set_cookie(
        resp, request, web_auth.SESSION_COOKIE, session_id, max_age=None
    )
    web_auth.set_cookie(
        resp,
        request,
        web_auth.DEVICE_COOKIE,
        token,
        max_age=web_auth.DEVICE_COOKIE_MAX_AGE,
    )
    return resp


def _setup_codes_page(
    request: Request,
    owner,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    headers: dict | None = None,
):
    # This page (and any re-render of it) always carries the plaintext
    # emergency-code pool while it is pending — Copilot review,
    # web_interface/routes.py:647: a cacheable GET response would let a
    # browser or reverse proxy retain/replay it after Done, contrary to
    # the spec's one-time-display intent. no-store is unconditional
    # here rather than gated on `_pending_codes` being non-empty, so a
    # cache entry from before Done can never be served stale either.
    resp_headers = {"Cache-Control": "no-store", **(headers or {})}
    return templates.TemplateResponse(
        request,
        "setup_codes.html",
        context.template_context(
            request,
            codes=context._pending_codes,
            notice=notice,
            error=error,
            can_email=context._can_email_owner(owner),
        ),
        status_code=status_code,
        headers=resp_headers,
    )


@router.get(
    "/setup/codes",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
)
async def setup_codes_page(request: Request):
    if context.access_store is None:
        raise HTTPException(status_code=503, detail="Access store not loaded")
    # Once setup is finalized and nothing is left to show, there is
    # nothing this page can do — the plaintexts are gone by design.
    if context.access_store.setup_finalized and not context._pending_codes:
        return RedirectResponse("/", status_code=303)
    if not context._pending_codes:
        # First view only: a reload must show this same pool, never a
        # fresh one, or codes the owner already wrote down would be
        # silently invalidated.
        context._pending_codes = context.access_store.generate_emergency_codes()
    owner = web_auth.current_principal(request).user
    return _setup_codes_page(request, owner)


@router.post(
    "/setup/codes/email",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def email_setup_codes(request: Request):
    owner = web_auth.current_principal(request).user
    if not context._pending_codes or not context._can_email_owner(owner):
        return _setup_codes_page(
            request,
            owner,
            error="Email is not available; copy the codes above instead.",
        )
    gateway = context.config.communication.email_gateway
    body = (
        "These 20 emergency codes let you sign in to the vending "
        "machine dashboard on a browser it has never seen before, or "
        "authorise a new owner during an ownership transfer. Each code "
        "works exactly once. Keep them somewhere other than the "
        "machine itself — they exist for when the machine cannot be "
        "reached.\n\n" + "\n".join(context._pending_codes)
    )
    ok = await send_email(gateway, owner.email, "Vending machine emergency codes", body)
    if not ok:
        return _setup_codes_page(
            request,
            owner,
            error="Email could not be sent; copy the codes above instead.",
        )
    return _setup_codes_page(request, owner, notice=f"Codes emailed to {owner.email}.")


@router.post(
    "/setup/codes/done",
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def finish_setup(request: Request):
    if context.access_store is None:
        raise HTTPException(status_code=503, detail="Access store not loaded")
    context.access_store.finalize_setup()
    context._pending_codes = []
    if context.display_controller is not None:
        context.display_controller.clear_setup_code()
    return HTMLResponse("", headers={"HX-Redirect": "/"})


# --- Task 19: reviewing retained users after a completed transfer ---


def _review_candidates() -> list:
    """Every user except the (new) owner, in a stable order.

    Recomputed fresh on every request — never cached — since a Keep is
    a no-op on the store and a Remove deletes exactly one entry, so
    re-deriving this list from live state is always cheap and correct.
    """
    owner = context.access_store.owner()
    owner_id = owner.id if owner else None
    return sorted(
        (u for u in context.access_store.users.values() if u.id != owner_id),
        key=lambda u: (u.name, u.id),
    )


def _setup_review_response(request: Request, user):
    device_count = sum(
        1
        for d in context.access_store.devices.values()
        if user.id in d.trusted_user_ids
    )
    return templates.TemplateResponse(
        request,
        "setup_review_user.html",
        context.template_context(
            request,
            user=_user_row(user),
            device_count=device_count,
        ),
    )


def _user_row(user) -> dict:
    # A plain dict with only what a template needs — never pin_hash or
    # pin_salt, the same hazard web_auth.TemplateUser exists to avoid
    # for current_user (see its docstring).
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role.value,
        "disabled": user.disabled,
        "last_login_at": user.last_login_at,
    }


def _review_response_after(request: Request, order: list, user_id: str):
    """Render whichever candidate in *order* comes after *user_id*, or
    answer HX-Redirect to /setup/codes when that was the last one.

    *order* is the candidate list computed before the decision on
    *user_id* was applied — a Keep leaves it accurate as-is; a Remove
    only ever deletes *user_id* itself, so every later entry still
    resolves.
    """
    ids = [u.id for u in order]
    idx = ids.index(user_id) + 1
    while idx < len(ids):
        candidate = context.access_store.get_user(ids[idx])
        if candidate is not None:
            return _setup_review_response(request, candidate)
        idx += 1
    return HTMLResponse("", headers={"HX-Redirect": "/setup/codes"})


@router.get(
    "/setup/review",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
)
async def setup_review_page(request: Request):
    candidates = _review_candidates()
    if not candidates:
        return RedirectResponse("/setup/codes", status_code=303)
    # A reload always restarts from the first remaining user — fine,
    # because Keep is idempotent (spec's own allowance).
    return _setup_review_response(request, candidates[0])


@router.post(
    "/setup/review/{user_id}/keep",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def setup_review_keep(request: Request, user_id: str):
    order = _review_candidates()
    if user_id not in {u.id for u in order}:
        raise HTTPException(status_code=404, detail="No such user")
    # Keep is a no-op on the store: the user is simply not touched.
    return _review_response_after(request, order, user_id)


@router.post(
    "/setup/review/{user_id}/remove",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def setup_review_remove(request: Request, user_id: str):
    order = _review_candidates()
    if user_id not in {u.id for u in order}:
        raise HTTPException(status_code=404, detail="No such user")
    # Sessions end before the user record itself is deleted.
    context.access_store.end_sessions_for_user(user_id)
    context.access_store.delete_user(user_id)
    return _review_response_after(request, order, user_id)
