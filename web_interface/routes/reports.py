"""Reports area router: activity table with period selector (Task 9)."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from web_interface import auth as web_auth
from web_interface import context
from services.access import Permission
from web_interface.levels import LEVEL_REPORTS


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/reports",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_reports))],
    )
    async def reports_level(request: Request, period: int = Query(default=24)):
        """The Reports level with activity table.

        Period parameter accepts 24, 168, or 720 hours (24h/7d/30d). Invalid
        periods fall back to 24. If no event recorder is wired, render a
        neutral placeholder and status 200.
        """
        # If no recorder is wired, return the neutral placeholder inside the shell
        if not context.event_recorder:
            return templates.TemplateResponse(
                "reports.html",
                context.template_context(
                    request,
                    level=LEVEL_REPORTS,
                    summary=None,
                    average=None,
                    period=period,
                ),
            )

        # Fall back to 24 hours if period is invalid
        if period not in (24, 168, 720):
            period = 24

        # Offload synchronous SQLite calls to a worker thread
        summary = await asyncio.to_thread(context.event_recorder.get_summary, period)
        average = await asyncio.to_thread(
            context.event_recorder.get_historical_average, period
        )

        return templates.TemplateResponse(
            "reports.html",
            context.template_context(
                request,
                level=LEVEL_REPORTS,
                period=period,
                summary=summary,
                average=average,
            ),
        )

    return router
