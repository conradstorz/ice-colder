"""GET/POST /settings/machine."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_edge.web.routes.settings.common import save_or_rerender
from ice_ui.levels import (
    LEVEL_SETTINGS_MACHINE,
)
from ice_ui.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- Machine


def _render_machine_form(request: Request, *, error: str | None):
    physical = context.config.physical
    return templates.TemplateResponse(
        request,
        "settings_machine.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_MACHINE,
            machine_id=context.config.machine_id,
            name=physical.common_name,
            address=physical.location.address,
            notes=physical.location.notes or "",
            error=error,
        ),
    )


@router.get(
    "/settings/machine",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
)
async def machine_form(request: Request):
    return _render_machine_form(request, error=None)


@router.post(
    "/settings/machine",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_contacts)),
        Depends(context.require_htmx),
    ],
)
async def update_machine(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    notes: str = Form(""),
    # Accepted so a direct POST supplying it doesn't 422 — and ignored:
    # machine id is read-only (spec §8, brief resolution 10). A disabled
    # <input> is not a guard on its own, so this handler never reads it.
    machine_id: str | None = Form(None),
):
    physical = context.config.physical
    physical.common_name = name
    physical.location.address = address
    physical.location.notes = notes
    return save_or_rerender(
        "machine", lambda error: _render_machine_form(request, error=error)
    )
