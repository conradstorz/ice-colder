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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web_interface import context
from web_interface.context import (
    ensure_setup_mode,
    set_access_store,
    set_availability,
    set_config_object,
    set_display_controller,
    set_event_recorder,
    set_health_monitor,
    set_inventory_manager,
    set_vmc_instance,
)
from web_interface.routes import auth as auth_routes
from web_interface.routes import controls, health, home, inventory, legacy, products
from web_interface.routes import reports, screen, settings, tests_level, users

__all__ = [
    "attach_routes",
    "ensure_setup_mode",
    "set_access_store",
    "set_availability",
    "set_config_object",
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
    permission-gated router (home, legacy, screen, and the still-empty area
    stubs) is included before routes/auth.py's session-less public router,
    the same relative order `app.include_router(router)` then
    `app.include_router(public)` had before this split — route matching
    order can matter, so that order is preserved here.
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

    # Permission-gated routers first (the original `router`), the
    # session-less auth/setup router last (the original `public`).
    app.include_router(home.build_router(templates))
    app.include_router(legacy.build_router(templates))
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
