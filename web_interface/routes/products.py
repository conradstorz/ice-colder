"""The Products levels: /products, /products/new, /products/{sku},
/products/{sku}/catalog, /products/{sku}/placement, /products/{sku}/copy and
/products/{sku}/delete.

Replaces the old /inventory/* catalog and placement forms (routes/legacy.py),
which keep answering until Task 15 removes them — see
.superpowers/sdd/part2/task-7-brief.md.

The read-only rule this task exists to enforce (brief resolution 4): a
placement-only role (loader, tech) holds edit_placement but not
edit_catalog. They reach GET /products and GET /products/{sku} (gated on
either permission) and see name, price and kind as plain read-only text —
GET /products/{sku} renders no <input> at all — while GET and POST
/products/{sku}/catalog are gated on edit_catalog alone and 403 for them.
"""

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from services.access import Permission
from services.config_store import (
    add_product,
    delete_product,
    save_config,
    update_product,
)
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_PRODUCTS, LEVEL_PRODUCTS_NEW, Level


def _require_any(*permissions: Permission):
    """FastAPI dependency: a live session holding at least one of
    *permissions*.

    web_auth.require() requires every listed permission (AND semantics);
    GET /products and GET /products/{sku} must be reachable by
    edit_catalog OR edit_placement (brief: "gate edit_catalog or
    edit_placement"), which needs OR semantics that helper doesn't offer.
    Mirrors web_auth.require()'s own unauthenticated/403 shape exactly, so
    the two dependencies behave identically except for the AND/OR choice.
    """

    def dependency(request: Request) -> web_auth.Principal:
        principal = web_auth.current_principal(request)
        if principal is None:
            raise web_auth._unauthenticated(request)
        if not any(p in principal.perms for p in permissions):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    return dependency


def _locked_skus() -> dict[str, str]:
    """sku -> fault code, for every currently product-scoped active fault.

    Copied from routes/legacy.py's _locked_skus() (brief resolution 6):
    that module is not imported from, since Task 15 deletes it.
    """
    if not context.vmc_instance:
        return {}
    return {
        f["sku"]: f["code"]
        for f in context.vmc_instance.active_faults()
        if f["scope"] == "product"
    }


def _find_product(sku: str):
    return next((p for p in context.config.products if p.sku == sku), None)


def _get_or_404(sku: str):
    """A missing or deleted SKU is a shell 404 on every route that takes
    one (brief resolution 7) — never a bare JSON 404."""
    product = _find_product(sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No such product: {sku}")
    return product


def _inventory_count(sku: str) -> int:
    """Counts live in InventoryManager, never in the stale
    Product.inventory_count seed field (part 1's d58da37, brief resolution
    5). With no manager wired, nothing is tracked and no count is known."""
    if context.inventory_manager:
        return context.inventory_manager.get_count(sku)
    return 0


def _is_tracked(sku: str) -> bool:
    if context.inventory_manager:
        return context.inventory_manager.is_tracked(sku)
    return False


def _product_level(product) -> Level:
    """The parameterized /products/{sku} level, crumbed under Products.
    The product's name is the crumb title, falling back to the SKU when
    the name is empty (brief resolution 8)."""
    return Level.child(
        LEVEL_PRODUCTS, product.name or product.sku, f"/products/{product.sku}"
    )


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _render_product_form(
        request: Request,
        *,
        mode: str,
        source_sku: str,
        sku: str,
        name: str,
        price: float,
        kind: str,
        slot,
        inventory_count: int,
        tracked: bool,
        error: str | None,
    ):
        if mode == "copy" and source_sku:
            level = Level.child(LEVEL_PRODUCTS, "Copy", f"/products/{source_sku}/copy")
        else:
            level = LEVEL_PRODUCTS_NEW
        return templates.TemplateResponse(
            "product_form.html",
            context.template_context(
                request,
                level=level,
                mode=mode,
                source_sku=source_sku,
                sku=sku,
                name=name,
                price=price,
                kind=kind,
                slot=slot,
                inventory_count=inventory_count,
                tracked=tracked,
                error=error,
            ),
        )

    @router.get(
        "/products",
        response_class=HTMLResponse,
        dependencies=[
            Depends(_require_any(Permission.edit_catalog, Permission.edit_placement))
        ],
    )
    async def products_list(request: Request):
        locked = _locked_skus()
        rows = [
            {
                "sku": p.sku,
                "name": p.name,
                "price": p.price,
                "slot": p.slot,
                "kind": p.kind,
                "count": _inventory_count(p.sku),
                "locked_code": locked.get(p.sku),
            }
            for p in context.config.products
        ]
        return templates.TemplateResponse(
            "products.html",
            context.template_context(request, level=LEVEL_PRODUCTS, products=rows),
        )

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

        success = add_product(
            context.config, sku, name, price, slot=parsed_slot, kind=kind
        )
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

        return HTMLResponse("", headers={"HX-Redirect": f"/products/{sku}"})

    @router.get(
        "/products/{sku}",
        response_class=HTMLResponse,
        dependencies=[
            Depends(_require_any(Permission.edit_catalog, Permission.edit_placement))
        ],
    )
    async def product_detail(request: Request, sku: str):
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        product = _get_or_404(sku)
        count = _inventory_count(sku)
        tracked = _is_tracked(sku)
        locked_code = _locked_skus().get(sku)

        tiles = [
            {
                "title": "Catalog",
                "url": f"/products/{sku}/catalog",
                "icon": "products",
                "context": f"${product.price:.2f} · {product.kind}",
                "enabled": Permission.edit_catalog in perms,
                "coming_soon": False,
            },
            {
                "title": "Placement",
                "url": f"/products/{sku}/placement",
                "icon": "inventory",
                "context": f"Slot {product.slot} · {count} in stock",
                "enabled": Permission.edit_placement in perms,
                "coming_soon": False,
            },
        ]

        return templates.TemplateResponse(
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

    @router.get(
        "/products/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def catalog_form(request: Request, sku: str):
        product = _get_or_404(sku)
        level = Level.child(
            _product_level(product), "Catalog", f"/products/{sku}/catalog"
        )
        return templates.TemplateResponse(
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
                "product_catalog.html",
                context.template_context(
                    request,
                    level=level,
                    product=product,
                    error="Could not save changes.",
                ),
            )
        return HTMLResponse("", headers={"HX-Redirect": f"/products/{sku}"})

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
        if success and context.inventory_manager:
            context.inventory_manager.remove_sku(sku)
        return HTMLResponse("", headers={"HX-Redirect": "/products"})

    return router
