"""GET/POST /settings/web."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from services import config_store
from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_SETTINGS_WEB,
)
from web_interface.templating import templates


router = APIRouter()


# ---------------------------------------------------------------- Web


def _render_web_form(request: Request, *, error: str | None):
    web = context.config.web
    return templates.TemplateResponse(
        "settings_web.html",
        context.template_context(
            request,
            level=LEVEL_SETTINGS_WEB,
            host=web.host,
            port=web.port,
            trusted_proxies="\n".join(web.trusted_proxies),
            error=error,
        ),
    )


@router.get(
    "/settings/web",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
)
async def web_form(request: Request):
    return _render_web_form(request, error=None)


@router.post(
    "/settings/web",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_secrets)),
        Depends(context.require_htmx),
    ],
)
async def update_web(
    request: Request,
    trusted_proxies: str = Form(""),
    # Accepted so a direct POST supplying either doesn't 422 — and
    # ignored: host and port are read-only, restart-to-apply (spec §8,
    # brief resolution 10).
    host: str | None = Form(None),
    port: str | None = Form(None),
):
    proxies = [
        line.strip()
        for line in trusted_proxies.replace(",", "\n").splitlines()
        if line.strip()
    ]
    context.config.web.trusted_proxies = proxies
    try:
        config_store.save_config(context.config)
    except Exception as e:
        logger.error(f"settings/web: save_config failed: {e}")
        return _render_web_form(request, error=f"Could not save changes: {e}")
    return HTMLResponse("", headers={"HX-Redirect": "/settings/web"})
