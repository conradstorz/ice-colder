"""GET /settings: the Settings home tile grid."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_SETTINGS,
)
from ice_ui.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- /settings


@router.get(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require_any(Permission.edit_contacts, Permission.edit_secrets))
    ],
)
async def settings_home(request: Request):
    perms = web_auth.perms_of(request)
    has_contacts = Permission.edit_contacts in perms
    has_secrets = Permission.edit_secrets in perms
    tiles = [
        {
            "title": "Machine",
            "url": "/settings/machine",
            "icon": "settings",
            "context": "Name, location",
            "enabled": has_contacts,
        },
        {
            "title": "Contacts",
            "url": "/settings/contacts",
            "icon": "settings",
            "context": "Owner, technicians",
            "enabled": has_contacts,
        },
        {
            "title": "Payments",
            "url": "/settings/payments",
            "icon": "settings",
            "context": "Stripe, PayPal, MDB",
            "enabled": has_secrets,
        },
        {
            "title": "Comms",
            "url": "/settings/comms",
            "icon": "settings",
            "context": "Email, SMS gateways",
            "enabled": has_secrets,
        },
        {
            "title": "MQTT",
            "url": "/settings/mqtt",
            "icon": "settings",
            "context": "Broker, credentials",
            "enabled": has_secrets,
        },
        {
            "title": "Web",
            "url": "/settings/web",
            "icon": "settings",
            "context": "Host, port, proxies",
            "enabled": has_secrets,
        },
        {
            "title": "Reports",
            "url": "/settings/reports",
            "icon": "reports",
            "context": "Scheduled summary email",
            "enabled": has_contacts,
        },
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        context.template_context(request, level=LEVEL_SETTINGS, tiles=tiles),
    )
