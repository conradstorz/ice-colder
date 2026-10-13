from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from services.access import AccessError, OwnerExistsError, Permission, Role
from services.auth_policy import pin_problem
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_USERS,
    LEVEL_USERS_NEW,
    Level,
)
from ice_ui.templating import templates


router = APIRouter()


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


def _get_user_or_404(user_id: str):
    """An unknown user id is a shell 404 on every route that takes one
    (brief resolution 12) — never a bare JSON 404. Existence is checked
    before any owner guard, so a 404 for an unknown id never depends on
    ownership (matching legacy's device-guard ordering)."""
    user = context.access_store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


def _user_level(user) -> Level:
    return Level.child(LEVEL_USERS, user.name, f"/users/{user.id}")


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
        request,
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
        request,
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
    if role_enum is Role.owner and Permission.manage_ownership not in principal.perms:
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
        r for r in Role if r is not Role.owner or Permission.manage_ownership in perms
    ]
    device_count = sum(
        1
        for d in context.access_store.devices.values()
        if target.id in d.trusted_user_ids
    )
    return templates.TemplateResponse(
        request,
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


# --- /users/{user_id} and its POSTs (registration order: see __init__.py) ---


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
    if role_enum is Role.owner and Permission.manage_ownership not in principal.perms:
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
