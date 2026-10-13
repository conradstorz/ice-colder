"""The /health/logs page (last 50 lines of LOGS/vmc.log)."""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_HEALTH_LOGS,
)
from web_interface.templating import templates

router = APIRouter()


@router.get(
    "/health/logs",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_logs))],
)
async def logs_view(request: Request):
    # 50 lines per the spec (executor resolution 7); the old fragment's
    # 10 was never the spec value.
    lines = await asyncio.to_thread(context.tail, context.LOG_PATH, 50)
    return templates.TemplateResponse(
        "health_logs.html",
        context.template_context(request, level=LEVEL_HEALTH_LOGS, logs=lines),
    )
