"""GET /settings: the Settings home tile grid."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_SETTINGS,
)
from web_interface.templating import templates


router = APIRouter()


def _require_any(*permissions: Permission):
    """FastAPI dependency: a live session holding at least one of
    *permissions*. Copied from web_interface/routes/products.py (not
    imported from — brief resolution 13 scopes this module to its own
    files); web_auth.require() is AND-only and GET /settings needs OR
    semantics (edit_contacts or edit_secrets)."""

    def dependency(request: Request) -> web_auth.Principal:
        principal = web_auth.current_principal(request)
        if principal is None:
            raise web_auth._unauthenticated(request)
        if not any(p in principal.perms for p in permissions):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    return dependency


# ---------------------------------------------------------------- /settings


@router.get(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[
        Depends(_require_any(Permission.edit_contacts, Permission.edit_secrets))
    ],
)
async def settings_home(request: Request):
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    has_contacts = Permission.edit_contacts in perms
    has_secrets = Permission.edit_secrets in perms
    tiles = [
        {
            "title": "Machine",
            "url": "/settings/machine",
            "icon": "settings",
            "context": "Name, location",
            "enabled": has_contacts,
            "coming_soon": False,
        },
        {
            "title": "Contacts",
            "url": "/settings/contacts",
            "icon": "settings",
            "context": "Owner, technicians",
            "enabled": has_contacts,
            "coming_soon": False,
        },
        {
            "title": "Payments",
            "url": "/settings/payments",
            "icon": "settings",
            "context": "Stripe, PayPal, MDB",
            "enabled": has_secrets,
            "coming_soon": False,
        },
        {
            "title": "Comms",
            "url": "/settings/comms",
            "icon": "settings",
            "context": "Email, SMS gateways",
            "enabled": has_secrets,
            "coming_soon": False,
        },
        {
            "title": "MQTT",
            "url": "/settings/mqtt",
            "icon": "settings",
            "context": "Broker, credentials",
            "enabled": has_secrets,
            "coming_soon": False,
        },
        {
            "title": "Web",
            "url": "/settings/web",
            "icon": "settings",
            "context": "Host, port, proxies",
            "enabled": has_secrets,
            "coming_soon": False,
        },
        {
            "title": "Reports",
            "url": "/settings/reports",
            "icon": "reports",
            "context": "Scheduled summary email",
            "enabled": has_contacts,
            "coming_soon": False,
        },
    ]
    return templates.TemplateResponse(
        "settings.html",
        context.template_context(request, level=LEVEL_SETTINGS, tiles=tiles),
    )
