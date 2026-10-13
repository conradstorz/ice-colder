from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_DEVICES,
)
from ice_ui.templating import templates


router = APIRouter()


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


def _get_device_or_404(device_id: str):
    device = context.access_store.devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="No such device")
    return device


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
        request,
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
