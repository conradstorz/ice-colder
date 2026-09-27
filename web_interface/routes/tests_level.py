"""Area router for the Tests level (spec §2's /tests).

Task 4 of the dashboard-v2-shell plan adds the one route this module
needs as a child-level stub for base.html's shell: `GET /tests`, gated on
Permission.run_tests, rendering a minimal tests.html through the shell.
Task 11 replaces the placeholder body with the real test-run UI; per that
task's brief (executor resolution 2), no form, hx-post, or other route is
added here yet.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_TESTS


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/tests",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.run_tests))],
    )
    async def tests_level(request: Request):
        return templates.TemplateResponse(
            "tests.html", context.template_context(request, level=LEVEL_TESTS)
        )

    return router
