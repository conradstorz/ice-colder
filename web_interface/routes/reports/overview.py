"""The Reports landing page: GET /reports."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_REPORTS,
    LEVEL_REPORTS_COLLECTIONS,
    LEVEL_REPORTS_METHOD,
    LEVEL_REPORTS_PERIOD,
    LEVEL_REPORTS_PRODUCT,
)
from web_interface.templating import templates

router = APIRouter()

# The four sub-tiles design §3 adds below the Reports level's existing
# activity table -- shape matches partials/tile.html (health.html's own
# tiles), reused verbatim so no new Tailwind class is introduced. Every
# level here is already gated on view_reports by /reports itself, so every
# tile is always "enabled" for a viewer who can reach this page at all.
_REPORT_TILES = [
    {
        "title": LEVEL_REPORTS_PERIOD.title,
        "url": LEVEL_REPORTS_PERIOD.url,
        "icon": "reports",
        "context": "Revenue, vends and uptime by day, week or month",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_PRODUCT.title,
        "url": LEVEL_REPORTS_PRODUCT.url,
        "icon": "reports",
        "context": "Units and revenue per product",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_METHOD.title,
        "url": LEVEL_REPORTS_METHOD.url,
        "icon": "reports",
        "context": "Revenue split by payment method",
        "enabled": True,
        "coming_soon": False,
    },
    {
        "title": LEVEL_REPORTS_COLLECTIONS.title,
        "url": LEVEL_REPORTS_COLLECTIONS.url,
        "icon": "reports",
        "context": "The last 50 cash collections",
        "enabled": True,
        "coming_soon": False,
    },
]


# ------------------------------------------------------------------
# /reports (Task 9, unchanged)
# ------------------------------------------------------------------


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
                tiles=_REPORT_TILES,
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
            tiles=_REPORT_TILES,
        ),
    )
