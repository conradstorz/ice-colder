"""The /health/availability truth-table page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_HEALTH_AVAILABILITY,
)
from ice_ui.templating import templates

router = APIRouter()


def _availability_context_line(avail) -> str:
    """Availability tile's summary line on /health (resolution 8)."""
    if not avail:
        return "—"
    if avail.payment_enabled:
        return "Payment enabled"
    n = len(avail.payment_blocking_reasons())
    return f"Payment disabled ({n})"


@router.get(
    "/health/availability",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def availability_view(request: Request):
    avail = context.availability
    rows = avail.table() if avail else []
    payment_enabled = avail.payment_enabled if avail else None
    blocking_reasons = avail.payment_blocking_reasons() if avail else []
    per_kind = {
        "ice": avail.sale_available("ice") if avail else (None, []),
        "water": avail.sale_available("water") if avail else (None, []),
    }
    return templates.TemplateResponse(
        request,
        "health_availability.html",
        context.template_context(
            request,
            level=LEVEL_HEALTH_AVAILABILITY,
            rows=rows,
            payment_enabled=payment_enabled,
            blocking_reasons=blocking_reasons,
            per_kind=per_kind,
        ),
    )
