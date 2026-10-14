"""The catalog-editing routes: /products/new, /products/{sku}/catalog, /products/{sku}/copy and /products/{sku}/delete."""

from uuid import uuid4
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from ice_edge.access.access import Permission
from ice_edge.config.config_store import (
    add_product,
    delete_product,
    update_product,
)
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import Level
from ice_ui.templating import templates

from .helpers import (
    _get_or_404,
    _inventory_count,
    _is_tracked,
    _product_level,
    _render_product_form,
)

router = APIRouter()


@router.get(
    "/products/new",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
)
async def new_product_form(request: Request):
    random_sku = f"SKU-{uuid4().hex[:6].upper()}"
    return _render_product_form(
        request,
        mode="new",
        source_sku="",
        sku=random_sku,
        name="",
        price=0.0,
        kind="other",
        slot=None,
        inventory_count=0,
        tracked=False,
        error=None,
    )


@router.post(
    "/products/new",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_catalog)),
        Depends(context.require_htmx),
    ],
)
async def create_product(
    request: Request,
    sku: str = Form(...),
    name: str = Form(...),
    price: float = Form(...),
    slot: str | None = Form(None),
    kind: str = Form("other"),
    inventory_count: int = Form(0),
    track_inventory: str | None = Form(None),
    mode: str = Form("new"),
    source_sku: str = Form(""),
):
    tracked = track_inventory is not None

    if inventory_count < 0:
        logger.warning(
            f"Cannot add product SKU={sku}: inventory_count "
            f"{inventory_count} is invalid (must be >= 0)"
        )
        return _render_product_form(
            request,
            mode=mode,
            source_sku=source_sku,
            sku=sku,
            name=name,
            price=price,
            kind=kind,
            slot=slot,
            inventory_count=inventory_count,
            tracked=tracked,
            error="Inventory count cannot be negative.",
        )

    try:
        parsed_slot = int(slot) if slot not in (None, "") else None
    except ValueError:
        return _render_product_form(
            request,
            mode=mode,
            source_sku=source_sku,
            sku=sku,
            name=name,
            price=price,
            kind=kind,
            slot=slot,
            inventory_count=inventory_count,
            tracked=tracked,
            error="Slot must be a whole number.",
        )

    success = add_product(context.config, sku, name, price, slot=parsed_slot, kind=kind)
    if not success:
        return _render_product_form(
            request,
            mode=mode,
            source_sku=source_sku,
            sku=sku,
            name=name,
            price=price,
            kind=kind,
            slot=slot,
            inventory_count=inventory_count,
            tracked=tracked,
            error=(
                "Could not add product — the SKU may already exist, or "
                "the slot is invalid or already in use."
            ),
        )

    if context.inventory_manager:
        context.inventory_manager.add_sku(sku, inventory_count, tracked=tracked)
    if context.machine_instance:
        context.machine_instance.gate.catalog_changed()

    return HTMLResponse("", headers={"HX-Redirect": f"/products/{sku}"})


@router.get(
    "/products/{sku}/catalog",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
)
async def catalog_form(request: Request, sku: str):
    product = _get_or_404(sku)
    level = Level.child(_product_level(product), "Catalog", f"/products/{sku}/catalog")
    return templates.TemplateResponse(
        request,
        "product_catalog.html",
        context.template_context(request, level=level, product=product, error=None),
    )


@router.post(
    "/products/{sku}/catalog",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_catalog)),
        Depends(context.require_htmx),
    ],
)
async def update_catalog(
    request: Request,
    sku: str,
    name: str = Form(...),
    price: float = Form(...),
    kind: str = Form("other"),
):
    # This endpoint owns name/price/kind only. slot=None leaves the
    # stored slot untouched (services/config_store.py update_product) —
    # a catalog POST can never smuggle a placement change through,
    # regardless of what a hostile form body contains (mirrors part 1's
    # a2b8829 guarantee for the placement side).
    product = _get_or_404(sku)
    success = update_product(context.config, sku, name, price, slot=None, kind=kind)
    if not success:
        # The only way update_product can fail with slot=None is an
        # unknown SKU — a race with a concurrent delete, since
        # _get_or_404 above already confirmed it existed this request.
        level = Level.child(
            _product_level(product), "Catalog", f"/products/{sku}/catalog"
        )
        return templates.TemplateResponse(
            request,
            "product_catalog.html",
            context.template_context(
                request,
                level=level,
                product=product,
                error="Could not save changes.",
            ),
        )
    if context.machine_instance:
        context.machine_instance.gate.catalog_changed()
    return HTMLResponse("", headers={"HX-Redirect": f"/products/{sku}"})


@router.get(
    "/products/{sku}/copy",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
)
async def copy_product_form(request: Request, sku: str):
    base = _get_or_404(sku)
    new_sku = f"SKU-{uuid4().hex[:6].upper()}"
    return _render_product_form(
        request,
        mode="copy",
        source_sku=sku,
        sku=new_sku,
        name=f"{base.name} Copy",
        price=base.price,
        kind=base.kind,
        slot=None,
        inventory_count=_inventory_count(sku),
        tracked=_is_tracked(sku),
        error=None,
    )


@router.post(
    "/products/{sku}/delete",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.edit_catalog)),
        Depends(context.require_htmx),
    ],
)
async def delete_product_route(request: Request, sku: str):
    _get_or_404(sku)
    success = delete_product(context.config, sku)
    if success:
        if context.inventory_manager:
            context.inventory_manager.remove_sku(sku)
        if context.machine_instance:
            context.machine_instance.gate.catalog_changed()
    return HTMLResponse("", headers={"HX-Redirect": "/products"})
