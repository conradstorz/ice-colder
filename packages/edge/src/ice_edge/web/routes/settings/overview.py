"""GET /settings: the Settings home tile grid."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_SETTINGS,
    LEVEL_SETTINGS_COMMS,
    LEVEL_SETTINGS_CONTACTS,
    LEVEL_SETTINGS_MACHINE,
    LEVEL_SETTINGS_MQTT,
    LEVEL_SETTINGS_PAYMENTS,
    LEVEL_SETTINGS_REPORTS,
    LEVEL_SETTINGS_WEB,
)
from ice_ui.templating import templates
from ice_ui.tiles import tile


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
        tile(
            "Machine",
            LEVEL_SETTINGS_MACHINE.url,
            "settings",
            "Name, location",
            has_contacts,
        ),
        tile(
            "Contacts",
            LEVEL_SETTINGS_CONTACTS.url,
            "settings",
            "Owner, technicians",
            has_contacts,
        ),
        tile(
            "Payments",
            LEVEL_SETTINGS_PAYMENTS.url,
            "settings",
            "Stripe, PayPal, MDB",
            has_secrets,
        ),
        tile(
            "Comms",
            LEVEL_SETTINGS_COMMS.url,
            "settings",
            "Email, SMS gateways",
            has_secrets,
        ),
        tile(
            "MQTT",
            LEVEL_SETTINGS_MQTT.url,
            "settings",
            "Broker, credentials",
            has_secrets,
        ),
        tile(
            "Web",
            LEVEL_SETTINGS_WEB.url,
            "settings",
            "Host, port, proxies",
            has_secrets,
        ),
        tile(
            "Reports",
            LEVEL_SETTINGS_REPORTS.url,
            "reports",
            "Scheduled summary email",
            has_contacts,
        ),
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        context.template_context(request, level=LEVEL_SETTINGS, tiles=tiles),
    )
