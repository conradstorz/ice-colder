"""Home (`/`) and the three fragment endpoints: `/status`, `/kpi` and `/pill`.

These are the only polling endpoints in the dashboard — the hero every 1 s,
the KPI cards every 60 s, and the health pill every 5 s. Every other area
lives in its own sibling module (`health.py`, `products.py`, `inventory.py`,
`reports.py`, `controls.py`, `tests_level.py`, `users.py`, `settings.py`),
assembled by `routes/__init__.py::attach_routes`.
"""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_CONTROLS,
    LEVEL_HEALTH,
    LEVEL_HOME,
    LEVEL_INVENTORY,
    LEVEL_PRODUCTS,
    LEVEL_REPORTS,
    LEVEL_SETTINGS,
    LEVEL_TESTS,
    LEVEL_USERS,
)

# The eight Home tiles (spec §2's permission table), as
# (title, url, icon key, permission set, coming_soon). Static — no live
# state — so it is a module-level constant rather than rebuilt per request;
# _build_tiles() below pairs each entry with its live context line and its
# per-request `enabled` flag.
_TILE_DEFS: list[tuple[str, str, str, frozenset, bool]] = [
    ("Health", LEVEL_HEALTH.url, "health", frozenset({Permission.view_status}), False),
    (
        "Products",
        LEVEL_PRODUCTS.url,
        "products",
        frozenset({Permission.edit_catalog, Permission.edit_placement}),
        False,
    ),
    (
        "Inventory",
        LEVEL_INVENTORY.url,
        "inventory",
        frozenset({Permission.edit_placement}),
        False,
    ),
    (
        "Reports",
        LEVEL_REPORTS.url,
        "reports",
        frozenset({Permission.view_reports}),
        False,
    ),
    (
        "Controls",
        LEVEL_CONTROLS.url,
        "controls",
        frozenset({Permission.machine_controls}),
        False,
    ),
    ("Tests", LEVEL_TESTS.url, "tests", frozenset({Permission.run_tests}), False),
    (
        "Users",
        LEVEL_USERS.url,
        "users",
        frozenset({Permission.manage_users}),
        False,
    ),
    (
        "Settings",
        LEVEL_SETTINGS.url,
        "settings",
        frozenset({Permission.edit_contacts, Permission.edit_secrets}),
        False,
    ),
]

# Rank highest-first for the pill's "which fault do I name" choice (Task 5
# brief, "Fault severity ranking"): critical > lockout > vend_failed >
# product_unavailable > warning > info. Values are the string values of
# contracts.vending_machine.Severity; kept as plain strings here (rather
# than importing the enum) since active_faults() already hands back
# `severity` as that enum's `.value`.
_SEVERITY_RANK = {
    "critical": 0,
    "lockout": 1,
    "vend_failed": 2,
    "product_unavailable": 3,
    "warning": 4,
    "info": 5,
}


def _fault_count_line() -> str:
    """Health tile context: the active-fault count, or an em dash when no
    VMC is wired (executor resolution 3 — every tile-context source may be
    missing, and this must never crash)."""
    if not context.vmc_instance:
        return "—"
    n = len(context.vmc_instance.active_faults())
    return f"{n} active fault{'s' if n != 1 else ''}"


def _product_count_line() -> str:
    """Products tile context: the product count."""
    if not context.config:
        return "—"
    n = len(context.config.products)
    return f"{n} product{'s' if n != 1 else ''}"


def _inventory_line() -> str:
    """Inventory tile context: the count of tracked products at or below
    LOW_STOCK_THRESHOLD, or the literal "tracking off" when nothing is
    tracked at all (executor resolution 1) — including when no
    InventoryManager is wired at all (executor resolution 2: "with no
    InventoryManager wired, treat nothing as tracked"). Counts always come
    from InventoryManager.is_tracked()/.get_count(), never from the stale
    Product.inventory_count seed field (executor resolution 2).
    """
    inv = context.inventory_manager
    if inv is None or not context.config:
        return "tracking off"
    tracked_skus = [p.sku for p in context.config.products if inv.is_tracked(p.sku)]
    if not tracked_skus:
        return "tracking off"
    low = sum(
        1 for sku in tracked_skus if inv.get_count(sku) <= context.LOW_STOCK_THRESHOLD
    )
    return f"{low} low"


def _user_count_line() -> str:
    """Users tile context: the enabled-user count."""
    if not context.access_store:
        return "—"
    n = len(context.access_store.enabled_users())
    return f"{n} user{'s' if n != 1 else ''}"


def _build_tiles(perms: frozenset) -> list[dict]:
    """Build all eight tile dicts. Every tile is always present in the
    returned list — `enabled` (True when *perms* intersects the tile's
    permission set) is what home.html uses to decide whether to render it,
    per spec §2's per-role table (Task 5 brief, "Tile visibility")."""
    context_lines = {
        "Health": _fault_count_line(),
        "Products": _product_count_line(),
        "Inventory": _inventory_line(),
        "Reports": "Sales & activity reports",
        "Controls": "Restart, reset, shutdown",
        "Tests": "Diagnostics & self-tests",
        "Users": _user_count_line(),
        "Settings": "Contacts, payments, comms",
    }
    return [
        {
            "title": title,
            "url": url,
            "icon": icon,
            "context": context_lines[title],
            "enabled": bool(perms & perm_set),
            "coming_soon": coming_soon,
        }
        for title, url, icon, perm_set, coming_soon in _TILE_DEFS
    ]


def _highest_priority_fault(active_faults: list[dict]) -> dict | None:
    """The fault the pill should name: highest severity first, ties broken
    by active_faults()'s own order (product faults before machine faults —
    controller/vmc.py:422). `min()` is stable, so among equal-ranked
    entries it returns the first one encountered, which is exactly that
    tie-break (Task 5 brief, "Fault severity ranking")."""
    if not active_faults:
        return None
    return min(
        active_faults,
        key=lambda f: _SEVERITY_RANK.get(f["severity"], len(_SEVERITY_RANK)),
    )


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def dashboard(request: Request):
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        tiles = _build_tiles(perms)
        return templates.TemplateResponse(
            "home.html",
            context.template_context(request, level=LEVEL_HOME, tiles=tiles),
        )

    @router.get(
        "/pill",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def pill(request: Request):
        """The bar's health indicator (spec §1.1), sharing
        context.health_snapshot() with /status so the two can never
        disagree about what "healthy" means (Task 5 brief). Tolerates a
        missing VMC exactly as /status does: a neutral grey pill, never a
        500 (executor resolution 3)."""
        snap = await context.health_snapshot()
        if snap["vmc_missing"]:
            pill_state, pill_text = "neutral", "—"
        elif snap["is_healthy"]:
            pill_state, pill_text = "ok", "OK"
        else:
            top = _highest_priority_fault(snap["active_faults"])
            pill_state, pill_text = "fault", (top["code"] if top else "ISSUE")
        return templates.TemplateResponse(
            "partials/pill.html",
            context.template_context(
                request, pill_state=pill_state, pill_text=pill_text
            ),
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
