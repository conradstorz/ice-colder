"""Controls level: restart, reset, shutdown with two-tap server-rendered confirm."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services import fsm_control
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_CONTROLS
from services.access import Permission


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/controls",
        dependencies=[Depends(web_auth.require(Permission.machine_controls))],
    )
    async def get_controls(request: Request):
        """Render the Controls level with three command buttons."""
        return templates.TemplateResponse(
            "controls.html",
            context.template_context(request, level=LEVEL_CONTROLS),
        )

    @router.get(
        "/controls/confirm/{command}",
        dependencies=[Depends(web_auth.require(Permission.machine_controls))],
    )
    async def get_confirm(request: Request, command: str):
        """Render the confirm variant of a command button.

        The confirming query parameter controls which state to render:
        - absent or truthy: render the confirm variant (Confirm/Cancel pair)
        - "false": render the plain first-tap button

        Renders partials/confirm_command.html directly — a fragment, not
        controls.html (which `{% extends "base.html" %}`) — so the response
        is exactly the `<div id="confirm-{command}">...</div>` htmx is
        swapping in via hx-target/hx-swap="outerHTML", not a whole
        <html>/<head>/<body> document nesting a second <main> inside the
        page's own on every confirm and Cancel tap.
        """
        confirming = request.query_params.get("confirming", "true").lower() != "false"

        return templates.TemplateResponse(
            "partials/confirm_command.html",
            context.template_context(
                request,
                level=LEVEL_CONTROLS,
                command=command,
                confirming=confirming,
            ),
        )

    @router.post(
        "/controls/{command}",
        dependencies=[
            Depends(web_auth.require(Permission.machine_controls)),
            Depends(context.require_htmx),
        ],
    )
    async def post_command(request: Request, command: str):
        """Execute a machine control command and return the result message."""
        result = fsm_control.perform_command(command, context.vmc_instance)
        return HTMLResponse(f"<p>{result}</p>")

    return router
