"""Home (`/`) and the three fragment endpoints: `/status`, `/kpi` and `/pill`.

These are the only polling endpoints in the dashboard — the hero every 1 s,
the KPI cards every 60 s, and the health pill every 5 s. Every other area
lives in its own sibling module (`health/`, `products/`, `inventory.py`,
`reports/`, `controls.py`, `tests_level/`, `users/`, `settings/`),
assembled by `routes/__init__.py::attach_routes`.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.views.health import build_kpi_summary
from ice_edge.web import context
from ice_ui.levels import LEVEL_HOME
from ice_ui.templating import templates
from ice_ui.tiles import build_tiles, fault_count_line
from ice_ui.views.health import pill_state


def _fault_count_line() -> str:
    """Health tile context: the active-fault count, or an em dash when no
    VMC is wired (executor resolution 3 — every tile-context source may be
    missing, and this must never crash)."""
    if not context.vmc_instance:
        return fault_count_line(None)
    return fault_count_line(len(context.vmc_instance.active_faults()))


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


def _tile_lines() -> dict[str, str]:
    """Each Home tile's live context line, keyed by tile title."""
    return {
        "Health": _fault_count_line(),
        "Products": _product_count_line(),
        "Inventory": _inventory_line(),
        "Reports": "Sales & activity reports",
        "Controls": "Restart, reset, shutdown",
        "Tests": "Diagnostics & self-tests",
        "Users": _user_count_line(),
        "Settings": "Contacts, payments, comms",
    }


router = APIRouter()


@router.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def dashboard(request: Request):
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    tiles = build_tiles(perms, _tile_lines())
    return templates.TemplateResponse(
        request,
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
    state, text = pill_state(snap)
    return templates.TemplateResponse(
        request,
        "partials/pill.html",
        context.template_context(request, pill_state=state, pill_text=text),
    )


@router.get(
    "/status",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def status_fragment(request: Request):
    return await context._render_status(request)


@router.get(
    "/kpi",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def kpi_fragment(request: Request):
    kpi = await build_kpi_summary(context.event_recorder)
    return templates.TemplateResponse(
        request,
        "partials/kpi_fragment.html",
        context.template_context(request, summary=kpi.summary, average=kpi.average),
    )
