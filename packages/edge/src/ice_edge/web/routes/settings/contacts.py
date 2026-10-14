"""GET/POST /settings/contacts."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from ice_common.config_model import Channel
from ice_edge.config import config_store
from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_SETTINGS_CONTACTS,
)
from ice_ui.templating import templates


router = APIRouter()


def _channels_from_form(values: list[str]) -> list[Channel]:
    """Filter posted channel checkbox values down to valid Channel members,
    silently dropping anything else rather than raising on a stray value."""
    valid = {c.value for c in Channel}
    return [Channel(v) for v in values if v in valid]


# ---------------------------------------------------------------- Contacts


def _render_contacts_form(request: Request, *, error: str | None):
    people = context.config.physical.people
    return templates.TemplateResponse(
        request,
        "settings_contacts.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_CONTACTS,
            owner=people.machine_owner,
            location_owner=people.location_owner,
            technicians=people.service_technicians,
            channels=list(Channel),
            error=error,
        ),
    )


@router.get(
    "/settings/contacts",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
)
async def contacts_form(request: Request):
    return _render_contacts_form(request, error=None)


@router.post(
    "/settings/contacts",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_contacts)),
        Depends(context.require_htmx),
    ],
)
async def update_contacts(
    request: Request,
    owner_name: str = Form(...),
    owner_email: str = Form(...),
    owner_phone: str = Form(""),
    owner_address: str = Form(""),
    owner_notes: str = Form(""),
    owner_channels: list[str] = Form([]),
    loc_name: str = Form(...),
    loc_email: str = Form(...),
    loc_phone: str = Form(""),
    loc_address: str = Form(""),
    loc_notes: str = Form(""),
    loc_channels: list[str] = Form([]),
):
    people = context.config.physical.people
    owner = people.machine_owner
    owner.name = owner_name
    owner.email = owner_email
    owner.phone = owner_phone
    owner.address = owner_address
    owner.notes = owner_notes
    owner.preferred_comm = _channels_from_form(owner_channels)

    loc = people.location_owner
    loc.name = loc_name
    loc.email = loc_email
    loc.phone = loc_phone
    loc.address = loc_address
    loc.notes = loc_notes
    loc.preferred_comm = _channels_from_form(loc_channels)

    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/contacts: save_config failed: {e}")
        return _render_contacts_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/contacts"})
