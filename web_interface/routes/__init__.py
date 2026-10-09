"""Route package for the web dashboard: one module per area.

Splits what used to be a single web_interface/routes.py (two routers built
inside one attach_routes() closure) into an area-per-module package. Shared
module state, its setters, and template_context now live in
web_interface/context.py; this package re-exports the setters and
ensure_setup_mode below so `from web_interface import routes` keeps working
unchanged for main.py and existing tests — see
.superpowers/sdd/part2/task-1-brief.md, Task 1.
"""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from web_interface import context
from web_interface.context import (
    ensure_setup_mode,
    set_access_store,
    set_availability,
    set_command_dispatcher,
    set_config_object,
    set_dispenser_profiles,
    set_display_controller,
    set_event_recorder,
    set_health_monitor,
    set_inventory_manager,
    set_vmc_instance,
)
from web_interface.routes import auth as auth_routes
from web_interface.routes import controls, health, home, inventory, products
from web_interface.routes import reports, screen, settings, tests_level, users

__all__ = [
    "attach_routes",
    "ensure_setup_mode",
    "set_access_store",
    "set_availability",
    "set_command_dispatcher",
    "set_config_object",
    "set_dispenser_profiles",
    "set_display_controller",
    "set_event_recorder",
    "set_health_monitor",
    "set_inventory_manager",
    "set_vmc_instance",
]


def attach_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    """Build every area router, wire them into *app*, and register the
    access-gate middleware exactly once.

    Registration order mirrors the original single-file attach_routes: every
    permission-gated router (home, screen, and the area modules) is included
    before routes/auth.py's session-less public router, the same relative
    order `app.include_router(router)` then `app.include_router(public)` had
    before this split — route matching order can matter, so that order is
    preserved here.
    """

    # Runs before every request (context.access_store is read live, so this
    # reflects whatever set_access_store() last set). /static/* is excluded
    # — it's a Starlette Mount that can't take dependencies, and the wizard
    # and error page both need it unstyled.
    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            return await call_next(request)

        store = context.access_store
        if store is not None:
            if store.corrupt:
                # Spec §6: a corrupt access file must never silently become
                # an open setup wizard, so every path — /setup included —
                # gets the same error page instead of just this one route
                # refusing to load.
                return HTMLResponse(
                    "<h1>Access file is corrupt</h1>"
                    "<p>The machine's access file could not be read, so the "
                    "dashboard is unavailable until an operator repairs or "
                    "removes it at the machine. The vending machine itself "
                    "keeps running.</p>",
                    status_code=503,
                )
            if store.setup_mode and path not in ("/setup", "/setup/codes"):
                return RedirectResponse("/setup", status_code=303)

        return await call_next(request)

    # In-shell 404/403 pages (spec §5): only these two statuses are ours to
    # render — everything else (in particular auth.py's unauthenticated-
    # request 401 + HX-Redirect and 303 + Location) must reach the client
    # exactly as raised, so it is delegated to FastAPI's own handler with
    # status, detail and headers intact. Registered once, here, so the
    # whole app gets it and server.py needs no change (Task 4 executor
    # resolution 6).
    #
    # Keyed on Starlette's own HTTPException, not fastapi.HTTPException:
    # Starlette's router raises the *base* class directly for "no route
    # matched" (a genuine 404, e.g. a mistyped URL), which is not an
    # instance of fastapi.HTTPException — registering on the subclass
    # would miss it. auth.py's require() raises fastapi.HTTPException,
    # which *is* an instance of this base class, so one handler catches
    # both.
    @app.exception_handler(StarletteHTTPException)
    async def _shell_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        if exc.status_code == 404:
            return templates.TemplateResponse(
                "error.html",
                context.template_context(
                    request,
                    level=None,
                    error_title="Not found",
                    error_message="The page you're looking for doesn't exist.",
                ),
                status_code=404,
            )
        if exc.status_code == 403:
            return templates.TemplateResponse(
                "error.html",
                context.template_context(
                    request,
                    level=None,
                    error_title="You don't have access to this",
                    error_message="Ask an owner or manager to grant access.",
                    # exc.detail carries the specific reason (e.g.
                    # require_htmx's "HTMX request required", or require()'s
                    # "Not permitted") — surfaced as a secondary line rather
                    # than swallowed, per the "never as bare JSON" mandate
                    # this is HTML, not a substitute for it.
                    error_detail=str(exc.detail) if exc.detail else None,
                ),
                status_code=403,
            )
        return await fastapi_http_exception_handler(request, exc)

    # Permission-gated routers first (the original `router`), the
    # session-less auth/setup router last (the original `public`).
    app.include_router(home.build_router(templates))
    app.include_router(screen.build_router(templates))
    app.include_router(health.build_router(templates))
    app.include_router(products.build_router(templates))
    app.include_router(inventory.build_router(templates))
    app.include_router(reports.build_router(templates))
    app.include_router(controls.build_router(templates))
    app.include_router(tests_level.build_router(templates))
    app.include_router(users.build_router(templates))
    app.include_router(settings.build_router(templates))
    app.include_router(auth_routes.build_router(templates))
