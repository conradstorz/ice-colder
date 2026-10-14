"""The /health landing page (four-tile summary plus build identity)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_HEALTH,
    LEVEL_HEALTH_AVAILABILITY,
    LEVEL_HEALTH_FAULTS,
    LEVEL_HEALTH_LOGS,
    LEVEL_HEALTH_SUBSYSTEMS,
)
from ice_ui.templating import templates
from ice_edge.web.routes.health.availability import _availability_context_line
from ice_edge.web.routes.health.faults import _faults_with_age
from ice_edge.web.routes.health.subsystems import _subsystem_summary

router = APIRouter()


@router.get(
    "/health",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def health_home(request: Request):
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    summary = context.health_monitor.get_summary() if context.health_monitor else None
    subsystems = _subsystem_summary()
    faults = _faults_with_age()
    ok = sum(1 for r in subsystems.values() if r["alive"] and not r["stale"])

    # Resolution 8: build all four sub-tile dicts with the keys
    # partials/tile.html expects, coming_soon false on all four --
    # Logs is the only one whose `enabled` depends on the viewer's
    # permissions rather than always being true.
    tiles = [
        {
            "title": "Subsystems",
            "url": LEVEL_HEALTH_SUBSYSTEMS.url,
            "icon": "health",
            "context": f"{ok}/{len(subsystems)} OK",
            "enabled": True,
            "coming_soon": False,
        },
        {
            "title": "Faults",
            "url": LEVEL_HEALTH_FAULTS.url,
            "icon": "health",
            "context": f"{len(faults)} active fault{'s' if len(faults) != 1 else ''}",
            "enabled": True,
            "coming_soon": False,
        },
        {
            "title": "Availability",
            "url": LEVEL_HEALTH_AVAILABILITY.url,
            "icon": "health",
            "context": _availability_context_line(context.availability),
            "enabled": True,
            "coming_soon": False,
        },
        {
            "title": "Logs",
            "url": LEVEL_HEALTH_LOGS.url,
            "icon": "health",
            "context": "Last 50 lines",
            "enabled": Permission.view_logs in perms,
            "coming_soon": False,
        },
    ]

    return templates.TemplateResponse(
        request,
        "health.html",
        context.template_context(
            request,
            level=LEVEL_HEALTH,
            tiles=tiles,
            mqtt_connected=summary["mqtt_connected"] if summary else None,
            vmc_state=summary["vmc_state"] if summary else None,
            vmc_build=summary["vmc"] if summary else None,
        ),
    )
