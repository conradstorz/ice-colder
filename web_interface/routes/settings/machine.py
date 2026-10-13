"""GET/POST /settings/machine."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from services import config_store
from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_SETTINGS_MACHINE,
)
from web_interface.templating import templates


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
    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/machine: save_config failed: {e}")
        return _render_machine_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/machine"})
