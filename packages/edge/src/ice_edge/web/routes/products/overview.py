"""The two pages a placement-only role may read: /products and /products/{sku}."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ice_contracts.money import format_cents
from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import LEVEL_PRODUCTS
from ice_ui.templating import templates

from .helpers import (
    _inventory_count,
    _is_tracked,
    _locked_skus,
    _product_level,
)

router = APIRouter()


@router.get(
    "/products",
    response_class=HTMLResponse,
    dependencies=[
        Depends(
            web_auth.require_any(Permission.edit_catalog, Permission.edit_placement)
        )
    ],
)
async def products_list(request: Request):
    locked = _locked_skus()
    rows = [
        {
            "sku": p.sku,
            "name": p.name,
            "price_cents": p.price_cents,
            "slot": p.slot,
            "kind": p.kind,
            "count": _inventory_count(p.sku),
            "locked_code": locked.get(p.sku),
        }
        for p in context.config.products
    ]
    return templates.TemplateResponse(
        request,
        "products.html",
        context.template_context(request, level=LEVEL_PRODUCTS, products=rows),
    )


@router.get(
    "/products/{sku}",
    response_class=HTMLResponse,
    dependencies=[
        Depends(
            web_auth.require_any(Permission.edit_catalog, Permission.edit_placement)
        )
    ],
)
async def product_detail(request: Request, sku: str):
    perms = web_auth.perms_of(request)
    product = context.product_or_404(sku)
    count = _inventory_count(sku)
    tracked = _is_tracked(sku)
    locked_code = _locked_skus().get(sku)

    tiles = [
        {
            "title": "Catalog",
            "url": f"/products/{sku}/catalog",
            "icon": "products",
            "context": f"{format_cents(product.price_cents)} · {product.kind}",
            "enabled": Permission.edit_catalog in perms,
        },
        {
            "title": "Placement",
            "url": f"/products/{sku}/placement",
            "icon": "inventory",
            "context": f"Slot {product.slot} · {count} in stock",
            "enabled": Permission.edit_placement in perms,
        },
    ]

    return templates.TemplateResponse(
        request,
        "product.html",
        context.template_context(
            request,
            level=_product_level(product),
            product=product,
            tiles=tiles,
            count=count,
            tracked=tracked,
            locked_code=locked_code,
        ),
    )
