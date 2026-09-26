"""The dashboard shell (`/`) and its live-updating `/status` and `/kpi`
fragments. Everything else (health, inventory, users, devices, activity,
logs, action, faults, ...) stays in routes/legacy.py for now — see Task 1
executor resolution 2 in .superpowers/sdd/part2/task-1-brief.md.
"""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def dashboard(request: Request):
        return templates.TemplateResponse(
            "dashboard.html", context.template_context(request)
        )

    @router.get(
        "/status",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def status_fragment(request: Request):
        return await context._render_status(templates, request)

    @router.get(
        "/kpi",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def kpi_fragment(request: Request):
        if context.event_recorder:
            summary = await asyncio.to_thread(context.event_recorder.get_summary, 24)
            average = await asyncio.to_thread(
                context.event_recorder.get_historical_average, 24
            )
        else:
            summary = None
            average = None
        return templates.TemplateResponse(
            "partials/kpi_fragment.html",
            context.template_context(request, summary=summary, average=average),
        )

    return router
