"""The Inventory restock level (spec §2 "Inventory row"): GET /inventory
and POST /inventory/{sku}/adjust — the screen a loader uses while standing
at the open machine with a box of stock, tapping counts up and down.

Deletes GET /inventory's old coverage in routes/legacy.py's
`inventory_view` (same commit — task-8 brief resolution 1): FastAPI
matches the first registered route, and web_interface/routes/__init__.py
includes legacy.build_router() before this module's, so leaving that old
handler in place would silently shadow this level's GET /inventory.
Every other /inventory/* route in legacy.py (add, new, copy, the
catalog/placement edit+update forms, delete) is untouched here; Task 15
retires the rest.

Counts live in InventoryManager, never in the stale Product.inventory_count
seed field (part 1's d58da37, brief resolution 4) — see _row_for below.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_INVENTORY

# The only four deltas the four adjust buttons ever send (brief resolution
# 6). The endpoint is reachable directly, not only from those buttons, so
# anything else is refused with 400 rather than trusted from the form.
_ALLOWED_DELTAS = frozenset({-10, -1, 1, 10})


def _find_product(sku: str):
    return next((p for p in context.config.products if p.sku == sku), None)


def _get_or_404(sku: str):
    """An unknown SKU is a shell 404 on both GET and POST (brief resolution
    9) — never a bare JSON 404."""
    product = _find_product(sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No such product: {sku}")
    return product


def _row_for(product) -> dict:
    """This row's template data. With no InventoryManager wired, nothing
    is tracked (brief resolution 4) — the page still renders, every
    product simply falls into the untracked group, rather than failing."""
    tracked = (
        context.inventory_manager.is_tracked(product.sku)
        if context.inventory_manager
        else False
    )
    row = {
        "sku": product.sku,
        "name": product.name,
        "slot": product.slot,
        "tracked": tracked,
    }
    if tracked:
        row["count"] = context.inventory_manager.get_count(product.sku)
    return row


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/inventory",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_placement))],
    )
    async def inventory_view(request: Request):
        rows = [_row_for(p) for p in context.config.products]
        # Tracked rows first, untracked at the bottom (brief resolution 8)
        # — decided once here, never left to the template to sort out.
        ordered = [r for r in rows if r["tracked"]] + [
            r for r in rows if not r["tracked"]
        ]
        return templates.TemplateResponse(
            "inventory.html",
            context.template_context(request, level=LEVEL_INVENTORY, rows=ordered),
        )

    @router.post(
        "/inventory/{sku}/adjust",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_placement)),
            Depends(context.require_htmx),
        ],
    )
    async def adjust_inventory(request: Request, sku: str, delta: str = Form(...)):
        product = _get_or_404(sku)

        # delta is declared as `str`, not `int` — FastAPI's own validation
        # would otherwise turn a non-integer form value into a 422 before
        # this handler ever runs, and the brief is explicit that a
        # rejected delta is a 400 (brief resolution 6). Parsed and
        # range-checked by hand instead, so a non-integer ("abc") and an
        # out-of-range integer ("7") both take the same path to the same
        # 400, leaving the stored count untouched.
        try:
            delta_value = int(delta)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="delta must be one of -10, -1, 1, 10"
            ) from exc
        if delta_value not in _ALLOWED_DELTAS:
            raise HTTPException(
                status_code=400, detail="delta must be one of -10, -1, 1, 10"
            )

        if context.inventory_manager:
            # Clamp at zero rather than erroring — a loader tapping -10 on
            # a count of 3 should land on 0, the physically meaningful
            # result (brief resolution 5). Deliberately different from the
            # placement form, which rejects a typed negative count outright.
            current = context.inventory_manager.get_count(sku)
            context.inventory_manager.set_count(sku, max(0, current + delta_value))

        # One row, not the whole list (brief resolution 7) — a loader's
        # repeated taps must not scroll-jump the page on a tablet.
        return templates.TemplateResponse(
            "partials/inventory_row.html",
            context.template_context(request, row=_row_for(product)),
        )

    return router
