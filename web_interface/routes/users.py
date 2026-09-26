"""The Users levels: /users, /users/{id}, /users/new, /devices,
/users/codes, /users/ownership and their POSTs (task-12 brief).

Replaces the whole of routes/legacy.py's users/devices surface (deleted from
that module in the same commit — task-12 brief resolution 1). The guard
functions below (`_guard_owner_target`, `_guard_owner_self_lockout`,
`_guard_owner_device`, `_check_owner_pin`) are moved verbatim from
routes/legacy.py, not reinvented — see each docstring for the Copilot-review
history that put it there. Every part-1 guarantee this module exists to
preserve is listed in task-12-report.md.

Route registration order matters within this router: literal path segments
("/users/new", "/users/codes", "/users/ownership") must be registered before
"/users/{user_id}" for the same HTTP method, or FastAPI/Starlette would match
the dynamic route first and capture e.g. "new" as a user id.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import AccessError, OwnerExistsError, Permission, Role
from services.auth_policy import pin_problem
from services.mailer import send_email
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_DEVICES,
    LEVEL_USERS,
    LEVEL_USERS_CODES,
    LEVEL_USERS_NEW,
    LEVEL_USERS_OWNERSHIP,
    Level,
)


def _guard_owner_target(principal: web_auth.Principal, user_id: str) -> None:
    """403 when *user_id* is the owner and the caller lacks manage_ownership.

    A secretary is the owner's delegate — manage_users lets them touch every
    other user, but the spec (§4) carves the owner out of that: any write
    whose target is the owner needs manage_ownership. Called first, before
    any store mutation, by every one of the four writes that take a user id
    (disable, enable, reset-pin, delete) plus the profile-edit route.
    Moved verbatim from routes/legacy.py (task-12 brief resolution 4).
    """
    owner = context.access_store.owner()
    if (
        owner is not None
        and owner.id == user_id
        and Permission.manage_ownership not in principal.perms
    ):
        raise HTTPException(status_code=403, detail="Not permitted")


def _guard_owner_self_lockout(principal: web_auth.Principal, user_id: str) -> None:
    """403 when *user_id* is the owner disabling or deleting themselves.

    _guard_owner_target only blocks a *non-owner* from targeting the owner;
    the owner always holds manage_ownership, so it let the owner disable or
    delete their own account. Deleting the sole owner leaves setup_finalized
    true with no owner, and ensure_setup_mode()'s begin_setup() then raises
    AccessError("setup has already been finalized") instead of recovering
    (Copilot review, web_interface/routes.py:1277). Disabling self is a
    milder version of the same lockout. Called only by disable and delete,
    in addition to _guard_owner_target; reset-pin and enable carry no such
    risk. Moved verbatim from routes/legacy.py (task-12 brief resolution 4).
    """
    owner = context.access_store.owner()
    if owner is not None and owner.id == user_id and principal.user.id == user_id:
        raise HTTPException(
            status_code=403, detail="The owner cannot do this to their own account"
        )


def _guard_owner_self_demotion(
    principal: web_auth.Principal, user_id: str, role_enum: Role
) -> None:
    """403 when the owner tries to change their own role away from
    Role.owner through the profile-edit route.

    _guard_owner_target only blocks a *non-owner* from targeting the
    owner; the owner always holds manage_ownership, so it let the owner
    demote themselves. services/access.py::update_user only blocks
    *creating a second* owner (it checks current_owner.id != user_id), not
    the sole owner giving theirs up. Doing so leaves the access store
    finalized but ownerless: no one holds manage_ownership, so emergency
    codes, ownership transfer and every owner-targeted write become
    permanently unreachable, and part 1's design gives a lost owner no
    software recovery path. This is the same hazard
    _guard_owner_self_lockout blocks for disable and delete; this closes
    the same door for a role change. A name/email edit on the owner's own
    row is unaffected — this only fires when the submitted role is not
    Role.owner. Task 12 fix round 1 (security review defect)."""
    owner = context.access_store.owner()
    if (
        owner is not None
        and owner.id == user_id
        and principal.user.id == user_id
        and role_enum is not Role.owner
    ):
        raise HTTPException(
            status_code=403, detail="The owner cannot change their own role"
        )


def _guard_owner_device(principal: web_auth.Principal, device) -> None:
    """403 when *device* trusts the owner and the caller lacks
    manage_ownership.

    Sibling to _guard_owner_target for the device case. Called after the
    caller has already confirmed *device* exists (a 404 for an unknown id
    must not depend on ownership), before any store mutation. Moved
    verbatim from routes/legacy.py (task-12 brief resolution 4).
    """
    owner = context.access_store.owner()
    if (
        owner is not None
        and owner.id in device.trusted_user_ids
        and Permission.manage_ownership not in principal.perms
    ):
        raise HTTPException(status_code=403, detail="Not permitted")


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


def _get_user_or_404(user_id: str):
    """An unknown user id is a shell 404 on every route that takes one
    (brief resolution 12) — never a bare JSON 404. Existence is checked
    before any owner guard, so a 404 for an unknown id never depends on
    ownership (matching legacy's device-guard ordering)."""
    user = context.access_store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


def _get_device_or_404(device_id: str):
    device = context.access_store.devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="No such device")
    return device


def _user_level(user) -> Level:
    return Level.child(LEVEL_USERS, user.name, f"/users/{user.id}")


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    # --- /users (the People list) -------------------------------------

    def _render_users_page(request: Request, *, notice: str | None = None):
        owner = context.access_store.owner()
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        users_sorted = sorted(context.access_store.users.values(), key=lambda u: u.name)
        rows = [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role.value,
                "disabled": u.disabled,
                "is_owner": owner is not None and u.id == owner.id,
            }
            for u in users_sorted
        ]
        can_own = Permission.manage_ownership in perms
        unused_codes = context.access_store.unused_emergency_code_count()
        pending_transfer = context.access_store.pending_transfer
        tiles = [
            {
                "title": "Devices",
                "url": "/devices",
                "icon": "users",
                "context": f"{len(context.access_store.devices)} known",
                "enabled": True,
                "coming_soon": False,
            },
            {
                "title": "Emergency codes",
                "url": "/users/codes",
                "icon": "settings",
                "context": f"{unused_codes} unused",
                "enabled": can_own,
                "coming_soon": False,
            },
            {
                "title": "Ownership",
                "url": "/users/ownership",
                "icon": "settings",
                "context": (
                    "Transfer pending" if pending_transfer else "Report & transfer"
                ),
                "enabled": can_own,
                "coming_soon": False,
            },
        ]
        return templates.TemplateResponse(
            "users.html",
            context.template_context(
                request,
                level=LEVEL_USERS,
                users=rows,
                tiles=tiles,
                unused_codes=unused_codes,
                pending_transfer=pending_transfer,
                notice=notice,
            ),
        )

    @router.get(
        "/users",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def users_list(request: Request):
        return _render_users_page(request)

    # --- /users/new -----------------------------------------------------

    def _render_new_user_form(
        request: Request,
        principal: web_auth.Principal,
        *,
        error: str | None = None,
        form: dict | None = None,
    ):
        # A secretary may create every role except owner — there is exactly
        # one owner, and only manage_ownership can mint one (spec §4.1).
        roles = [
            r
            for r in Role
            if r is not Role.owner or Permission.manage_ownership in principal.perms
        ]
        return templates.TemplateResponse(
            "user_form.html",
            context.template_context(
                request,
                level=LEVEL_USERS_NEW,
                roles=roles,
                error=error,
                form=form or {"name": "", "email": "", "role": ""},
            ),
        )

    @router.get(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def new_user_form(request: Request):
        principal = web_auth.current_principal(request)
        return _render_new_user_form(request, principal)

    @router.post(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def create_user(
        request: Request,
        name: str = Form(...),
        email: str = Form(""),
        role: str = Form(...),
        pin: str = Form(...),
    ):
        principal = web_auth.current_principal(request)
        try:
            role_enum = Role(role)
        except ValueError:
            return _render_new_user_form(
                request,
                principal,
                error="Invalid role.",
                form={"name": name, "email": email, "role": role},
            )
        # A secretary submitting role=owner directly (bypassing the hidden
        # <option>) must still be refused server-side — the form only hides
        # the control, it is never the authority.
        if (
            role_enum is Role.owner
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

        problem = pin_problem(pin)
        if problem:
            return _render_new_user_form(
                request,
                principal,
                error=problem,
                form={"name": name, "email": email, "role": role},
            )

        try:
            context.access_store.create_user(name, email or None, role_enum, pin)
        except OwnerExistsError:
            return _render_new_user_form(
                request,
                principal,
                error="This machine already has an owner.",
                form={"name": name, "email": email, "role": role},
            )
        # Re-renders the list (not a redirect to the new person's page) so
        # the created name is visible in the same response — matches part
        # 1's behavior exactly, which several of its tests assert on
        # directly (task-12 brief resolution 3).
        return _render_users_page(request, notice=f"{name} added.")

    # --- /users/{user_id} (the Person page) -----------------------------

    def _render_user_detail(
        request: Request,
        target,
        *,
        error: str | None = None,
        status_code: int = 200,
    ):
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        owner = context.access_store.owner()
        is_owner_target = owner is not None and owner.id == target.id
        is_self = is_owner_target and principal.user.id == target.id
        # A secretary viewing the owner's row: the only way is_owner_target
        # can be true while is_self is false, since manage_ownership (and
        # therefore ever being "the owner") is held by Role.owner alone.
        may_write = (not is_owner_target) or Permission.manage_ownership in perms
        roles = [
            r
            for r in Role
            if r is not Role.owner or Permission.manage_ownership in perms
        ]
        device_count = sum(
            1
            for d in context.access_store.devices.values()
            if target.id in d.trusted_user_ids
        )
        return templates.TemplateResponse(
            "user.html",
            context.template_context(
                request,
                level=_user_level(target),
                target=target,
                is_owner_target=is_owner_target,
                is_self=is_self,
                may_write=may_write,
                roles=roles,
                device_count=device_count,
                error=error,
            ),
            status_code=status_code,
        )

    @router.get(
        "/users/codes",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_ownership))],
    )
    async def codes_page(request: Request):
        return templates.TemplateResponse(
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
        ok = await send_email(
            gateway, owner.email, "Vending machine access report", report
        )
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
                error=(
                    "A transfer is already pending. Cancel it before starting another."
                ),
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

    # --- /devices ---------------------------------------------------------

    def _render_devices_page(request: Request, *, notice: str | None = None):
        owner = context.access_store.owner()
        devices = sorted(context.access_store.devices.values(), key=lambda d: d.label)
        user_names = {u.id: u.name for u in context.access_store.users.values()}
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        rows = []
        for d in devices:
            may_write = (
                owner is None
                or owner.id not in d.trusted_user_ids
                or Permission.manage_ownership in perms
            )
            rows.append(
                {
                    "id": d.id,
                    "label": d.label,
                    "shared": d.shared,
                    "trusted_names": [
                        user_names.get(uid)
                        for uid in d.trusted_user_ids
                        if uid in user_names
                    ],
                    "last_seen_at": d.last_seen_at,
                    "may_write": may_write,
                }
            )
        return templates.TemplateResponse(
            "devices.html",
            context.template_context(
                request, level=LEVEL_DEVICES, devices=rows, notice=notice
            ),
        )

    @router.get(
        "/devices",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def devices_list(request: Request):
        return _render_devices_page(request)

    @router.post(
        "/devices/{device_id}/forget",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def forget_device_route(request: Request, device_id: str):
        device = _get_device_or_404(device_id)
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        # Order matters: a forgotten device must not keep whoever is using
        # it logged in one request longer than necessary.
        context.access_store.end_sessions_for_device(device_id)
        context.access_store.forget_device(device_id)
        return HTMLResponse("", headers={"HX-Redirect": "/devices"})

    @router.post(
        "/devices/{device_id}/shared",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def toggle_device_shared_route(request: Request, device_id: str):
        device = _get_device_or_404(device_id)
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        context.access_store.set_device_shared(device_id, not device.shared)
        return HTMLResponse("", headers={"HX-Redirect": "/devices"})

    # --- /users/{user_id} and its POSTs (registered after every literal
    # /users/* path above, so this dynamic segment never shadows them) ----

    @router.get(
        "/users/{user_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def user_detail(request: Request, user_id: str):
        target = _get_user_or_404(user_id)
        return _render_user_detail(request, target)

    @router.post(
        "/users/{user_id}",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def update_user_route(
        request: Request,
        user_id: str,
        name: str = Form(...),
        email: str = Form(""),
        role: str = Form(...),
    ):
        target = _get_user_or_404(user_id)
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        try:
            role_enum = Role(role)
        except ValueError:
            return _render_user_detail(request, target, error="Invalid role.")
        _guard_owner_self_demotion(principal, user_id, role_enum)
        if (
            role_enum is Role.owner
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")
        try:
            context.access_store.update_user(
                user_id, name=name, email=email or None, role=role_enum
            )
        except OwnerExistsError:
            return _render_user_detail(
                request, target, error="This machine already has an owner."
            )
        return HTMLResponse("", headers={"HX-Redirect": f"/users/{user_id}"})

    @router.post(
        "/users/{user_id}/disable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def disable_user(request: Request, user_id: str):
        _get_user_or_404(user_id)
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            context.access_store.set_user_disabled(user_id, True)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        # A disabled user must not keep an open tab working until it idles
        # out on its own.
        context.access_store.end_sessions_for_user(user_id)
        return HTMLResponse("", headers={"HX-Redirect": f"/users/{user_id}"})

    @router.post(
        "/users/{user_id}/enable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def enable_user(request: Request, user_id: str):
        _get_user_or_404(user_id)
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        try:
            context.access_store.set_user_disabled(user_id, False)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        return HTMLResponse("", headers={"HX-Redirect": f"/users/{user_id}"})

    @router.post(
        "/users/{user_id}/reset-pin",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def reset_user_pin(request: Request, user_id: str, pin: str = Form(...)):
        target = _get_user_or_404(user_id)
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        problem = pin_problem(pin)
        if problem:
            return _render_user_detail(request, target, error=problem)
        try:
            # set_user_pin rehashes and drops the user from every device, so
            # the next login re-enrolls (spec §4.1) — AccessStore already
            # does both halves of that.
            context.access_store.set_user_pin(user_id, pin)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        # set_user_pin only drops device *trust*; resolve_session() does not
        # re-check it, so a session opened before the reset would otherwise
        # keep working on its old device until it idles out on its own.
        context.access_store.end_sessions_for_user(user_id)
        return HTMLResponse("", headers={"HX-Redirect": f"/users/{user_id}"})

    @router.post(
        "/users/{user_id}/delete",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def delete_user_route(request: Request, user_id: str):
        _get_user_or_404(user_id)
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            context.access_store.delete_user(user_id)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        context.access_store.end_sessions_for_user(user_id)
        return HTMLResponse("", headers={"HX-Redirect": "/users"})

    return router
