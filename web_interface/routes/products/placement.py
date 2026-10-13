"""The placement routes: GET and POST /products/{sku}/placement."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from services.access import Permission
from services.config_store import (
    save_config,
    update_product,
)
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import Level
from web_interface.templating import templates

from .helpers import (
    _get_or_404,
    _inventory_count,
    _is_tracked,
    _product_level,
)

router = APIRouter()


@router.get(
    "/products/{sku}/placement",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_placement))],
)
async def placement_form(request: Request, sku: str):
    product = _get_or_404(sku)
    level = Level.child(
        _product_level(product), "Placement", f"/products/{sku}/placement"
    )
    return templates.TemplateResponse(
        "product_placement.html",
        context.template_context(
            request,
            level=level,
            product=product,
            inventory_count=_inventory_count(sku),
            tracked=_is_tracked(sku),
            error=None,
        ),
    )


@router.post(
    "/products/{sku}/placement",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_placement)),
        Depends(context.require_htmx),
    ],
)
async def update_placement(
    request: Request,
    sku: str,
    slot: int = Form(...),
    inventory_count: int = Form(...),
    track_inventory: str | None = Form(None),
):
    product = _get_or_404(sku)
    tracked = track_inventory is not None

    def _rerender(error: str) -> HTMLResponse:
        level = Level.child(
            _product_level(product), "Placement", f"/products/{sku}/placement"
        )
        return templates.TemplateResponse(
            "product_placement.html",
            context.template_context(
                request,
                level=level,
                product=product,
                inventory_count=_inventory_count(sku),
                tracked=_is_tracked(sku),
                error=error,
            ),
        )

    if inventory_count < 0:
        # Mirror services/config_store.py's slot < 0 guard: reject the
        # whole write and leave the stored count unchanged (part 1's
        # 231fe36) rather than let a negative stock level corrupt
        # InventoryManager.
        logger.warning(
            f"Cannot update placement SKU={sku}: inventory_count "
            f"{inventory_count} is invalid (must be >= 0)"
        )
        return _rerender("Inventory count cannot be negative.")

    # This endpoint owns slot/count/tracking only — pass the product's
    # own stored name/price through unchanged (kind=None leaves kind
    # untouched too) so a placement POST can never smuggle a catalog
    # change through, regardless of what a hostile form body contains
    # (part 1's a2b8829).
    slot_ok = update_product(
        context.config, sku, product.name, product.price, slot=slot, kind=None
    )
    if not slot_ok:
        # update_product returns False when the requested slot is
        # negative or already in use. A rejected slot change must leave
        # every field of this form — slot, count, and tracking —
        # exactly as it was, not just the slot (part 1's a2b8829,
        # Copilot review web_interface/routes.py:1202): checked before
        # any InventoryManager write below.
        return _rerender("That slot is already in use.")

    if context.inventory_manager:
        context.inventory_manager.add_sku(sku, inventory_count, tracked=tracked)
    else:
        product.inventory_count = inventory_count
        product.track_inventory = tracked
        save_config(context.config)
    if context.machine_instance:
        context.machine_instance.gate.catalog_changed()

    return HTMLResponse("", headers={"HX-Redirect": f"/products/{sku}"})
