"""The /health/subsystems list, /health/subsystems/{name} detail and its /live fragment."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ice_contracts.board.vending_machine import (
    EXPECTED_SUBSYSTEMS,
)
from ice_edge.access.access import Permission
from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_HEALTH_SUBSYSTEMS,
    Level,
)
from ice_ui.views.subsystem_window import build_window
from ice_ui.templating import templates

router = APIRouter()


def _subsystem_summary() -> dict[str, dict]:
    """One row per EXPECTED_SUBSYSTEMS entry: the live row from the health
    monitor when it has ever heard from that subsystem, else the neutral
    empty_subsystem_row() placeholder -- and the placeholder for every
    subsystem when no health monitor is wired at all (rule 3)."""
    live = (
        context.health_monitor.get_summary()["subsystems"]
        if context.health_monitor
        else {}
    )
    return {
        name: live.get(name, HealthMonitor.empty_subsystem_row())
        for name in EXPECTED_SUBSYSTEMS
    }


def _window_context(name: str) -> dict:
    """One board's live view model (subsystem-windows design §4.6): the
    row from `_subsystem_summary()`, its signals, the live `Availability`
    (may be `None`), and the machine's local tz. Shared by the page route
    and the `/live` fragment route so they can never disagree. `name` must
    already be validated against EXPECTED_SUBSYSTEMS by the caller."""
    row = _subsystem_summary()[name]
    monitor = context.health_monitor
    signals = monitor.get_summary()["signals"].get(name, {}) if monitor else {}
    temp_range = monitor.temp_range if monitor else (-20.0, 80.0)
    return build_window(
        row,
        signals,
        context.availability,
        temp_range=temp_range,
        now=time.time(),
        # Copilot review (PR 27, finding 2): datetime.now().astimezone().tzinfo
        # is a fixed offset for the current instant, so a timestamp on the
        # other side of a DST transition rendered an hour off. tz=None makes
        # build_window's datetime.fromtimestamp(ts, None) do the OS-local
        # conversion per timestamp instead -- the project convention
        # ("Production code uses the OS's real timezone resolver").
        tz=None,
    )


@router.get(
    "/health/subsystems",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def subsystems_view(request: Request):
    return templates.TemplateResponse(
        request,
        "health_subsystems.html",
        context.template_context(
            request, level=LEVEL_HEALTH_SUBSYSTEMS, subsystems=_subsystem_summary()
        ),
    )


@router.get(
    "/health/subsystems/{name}",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def subsystem_detail(request: Request, name: str):
    if name not in EXPECTED_SUBSYSTEMS:
        raise HTTPException(status_code=404, detail=f"Unknown subsystem '{name}'")
    row = _subsystem_summary()[name]
    level = Level.child(LEVEL_HEALTH_SUBSYSTEMS, name, f"/health/subsystems/{name}")
    return templates.TemplateResponse(
        request,
        "health_subsystem.html",
        context.template_context(
            request,
            level=level,
            name=name,
            row=row,
            window=_window_context(name),
        ),
    )


@router.get(
    "/health/subsystems/{name}/live",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def subsystem_live(request: Request, name: str):
    """The `#live` wrapper's poll target (subsystem-windows design
    §4.6): renders only `partials/subsystem_live.html`, with no page
    chrome and no `hx-` attribute of its own -- the wrapper in
    health_subsystem.html owns every htmx attribute. Listed in
    web_auth.POLLING_PATHS (via `is_polling_path`) so a 2s poll never
    refreshes the session's idle clock."""
    if name not in EXPECTED_SUBSYSTEMS:
        raise HTTPException(status_code=404, detail=f"Unknown subsystem '{name}'")
    return templates.TemplateResponse(
        request,
        "partials/subsystem_live.html",
        context.template_context(request, name=name, window=_window_context(name)),
    )
