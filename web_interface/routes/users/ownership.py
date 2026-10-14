from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.notify.mailer import send_email
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_USERS_CODES,
    LEVEL_USERS_OWNERSHIP,
)
from ice_ui.templating import templates


router = APIRouter()


def _check_owner_pin(
    request: Request, principal: web_auth.Principal, pin: str
) -> tuple[str, int | None] | None:
    """Verify the caller's own PIN; None on success, else (message,
    retry_after_seconds) on failure.

    Moved verbatim from routes/legacy.py (task-12 brief resolution 4). Used
    by the ownership-transfer flow (start/cancel), which spec §4 keeps
    PIN-gated. Regenerate uses the shell's two-tap confirm instead (task-12
    brief resolution 8) — see users_codes.html's PIN field, still checked
    server-side via this same function for defense in depth.
    """
    client = web_auth.client_key(request)
    trusted = web_auth.is_trusted_client(request, principal.user.id)
    remaining = web_auth.backoff.check(
        "pin", principal.user.id, client, trusted=trusted
    )
    if remaining is not None:
        retry_after = int(remaining) + 1
        return f"Too many attempts. Try again in {retry_after} s.", retry_after
    if not context.access_store.verify_user_pin(principal.user.id, pin):
        web_auth.backoff.record_failure(
            "pin", principal.user.id, client, trusted=trusted
        )
        return "Wrong PIN.", None
    web_auth.backoff.record_success("pin", principal.user.id, client, trusted=trusted)
    return None


@router.get(
    "/users/codes",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
)
async def codes_page(request: Request):
    return templates.TemplateResponse(
        request,
        "users_codes.html",
        context.template_context(
            request,
            level=LEVEL_USERS_CODES,
            unused_codes=context.access_store.unused_emergency_code_count(),
            confirming=False,
            new_codes=None,
            error=None,
        ),
    )


@router.get(
    "/users/codes/regenerate/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
)
async def codes_regenerate_confirm(
    request: Request, confirming: str | None = Query(default=None)
):
    """confirm_button.html's confirm_url contract (Task 5): absent or
    anything but the literal string "false" renders the confirming
    (Confirm/Cancel) state; "false" renders the plain first-tap button
    — this is what its Cancel button sends via hx-vals (brief
    resolution 8)."""
    return templates.TemplateResponse(
        request,
        "partials/codes_regenerate.html",
        context.template_context(
            request,
            confirming=(confirming != "false"),
            new_codes=None,
            error=None,
        ),
    )


@router.post(
    "/users/codes/regenerate",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def codes_regenerate(request: Request, pin: str = Form(...)):
    principal = web_auth.current_principal(request)
    problem = _check_owner_pin(request, principal, pin)
    # no-store unconditionally, matching legacy's own reasoning: the
    # same header applies every time this function is the response,
    # not just when new_codes is actually populated (task-12 brief
    # resolution: "Cache-Control: no-store on plaintext codes" —
    # part 1's 7fa2c14).
    headers = {"Cache-Control": "no-store"}
    if problem:
        message, retry_after = problem
        status_code = 200
        if retry_after is not None:
            status_code = 429
            headers["Retry-After"] = str(retry_after)
        return templates.TemplateResponse(
            request,
            "partials/codes_regenerate.html",
            context.template_context(
                request, confirming=False, new_codes=None, error=message
            ),
            status_code=status_code,
            headers=headers,
        )
    # Replaces the whole pool, used codes included; the old codes stop
    # working immediately (AccessStore.generate_emergency_codes).
    new_codes = context.access_store.generate_emergency_codes()
    return templates.TemplateResponse(
        request,
        "partials/codes_regenerate.html",
        context.template_context(
            request, confirming=False, new_codes=new_codes, error=None
        ),
        headers=headers,
    )


# --- /users/ownership -------------------------------------------------


def _render_ownership(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    transfer_code: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
):
    principal = web_auth.current_principal(request)
    owner = principal.user if principal else None
    # no-store unconditionally on every render of this page, the same
    # reasoning as codes_regenerate above — this is also the response
    # that shows a freshly-started transfer's plaintext code.
    resp_headers = {"Cache-Control": "no-store", **(headers or {})}
    return templates.TemplateResponse(
        request,
        "users_ownership.html",
        context.template_context(
            request,
            level=LEVEL_USERS_OWNERSHIP,
            pending_transfer=context.access_store.pending_transfer,
            can_email=context._can_email_owner(owner),
            owner_email=owner.email if owner else None,
            error=error,
            notice=notice,
            # Shown exactly once, in the response that created it —
            # never persisted, never re-rendered on a later request.
            transfer_code=transfer_code,
        ),
        status_code=status_code,
        headers=resp_headers,
    )


def _owner_pin_problem_ownership_response(
    request: Request, problem: tuple[str, int | None]
):
    message, retry_after = problem
    if retry_after is not None:
        return _render_ownership(
            request,
            error=message,
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return _render_ownership(request, error=message)


@router.get(
    "/users/ownership",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
)
async def ownership_page(request: Request):
    return _render_ownership(request)


@router.post(
    "/users/report",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def email_machine_report_route(request: Request):
    principal = web_auth.current_principal(request)
    owner = principal.user
    if not context._can_email_owner(owner):
        return _render_ownership(
            request,
            error="Email is not available; check the email gateway settings.",
        )
    gateway = context.config.communication.email_gateway
    # machine_report already omits every hash and PIN (spec §3.4).
    report = context.access_store.machine_report(context.config)
    ok = await send_email(gateway, owner.email, "Vending machine access report", report)
    if not ok:
        return _render_ownership(request, error="Email could not be sent.")
    return _render_ownership(request, notice=f"Report emailed to {owner.email}.")


@router.post(
    "/users/transfer",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def start_ownership_transfer(
    request: Request, pin: str = Form(...), emergency_code: str = Form(...)
):
    # Guard first, before the PIN check and before the code check, so a
    # re-entrant call while a transfer is already pending can spend
    # nothing (part 1's guarantee: "Starting a transfer leaves the
    # current owner in full control").
    if context.access_store.pending_transfer is not None:
        return _render_ownership(
            request,
            error=("A transfer is already pending. Cancel it before starting another."),
        )

    # Check first, consume only on success, never the other way round —
    # a wrong PIN must leave the emergency-code pool untouched (part 1's
    # guarantee).
    principal = web_auth.current_principal(request)
    problem = _check_owner_pin(request, principal, pin)
    if problem:
        return _owner_pin_problem_ownership_response(request, problem)

    client = web_auth.client_key(request)
    remaining = web_auth.backoff.check("transfer", "pool", client)
    if remaining is not None:
        return _render_ownership(
            request,
            error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
            status_code=429,
        )

    # consume_emergency_code only mutates the pool on a match, so a
    # wrong code both fails this check and consumes nothing.
    if not context.access_store.consume_emergency_code(
        emergency_code.strip(), principal.user.id, "transfer"
    ):
        web_auth.backoff.record_failure("transfer", "pool", client)
        return _render_ownership(request, error="That code was not accepted.")
    web_auth.backoff.record_success("transfer", "pool", client)

    # Nothing else changes here: the current owner stays fully in
    # control until the incoming owner completes the wizard.
    transfer_code = context.access_store.start_transfer(principal.user.id)
    return _render_ownership(
        request,
        notice="Ownership transfer started. Give this code to the new owner.",
        transfer_code=transfer_code,
    )


@router.post(
    "/users/transfer/cancel",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.manage_ownership)),
        Depends(context.require_htmx),
    ],
)
async def cancel_ownership_transfer(request: Request, pin: str = Form(...)):
    principal = web_auth.current_principal(request)
    problem = _check_owner_pin(request, principal, pin)
    if problem:
        return _owner_pin_problem_ownership_response(request, problem)
    context.access_store.cancel_transfer()
    return _render_ownership(request, notice="Ownership transfer cancelled.")
