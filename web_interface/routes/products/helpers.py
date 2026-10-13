"""Helpers shared by the Products sub-modules; no routes of its own."""

from fastapi import HTTPException, Request

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_PRODUCTS, LEVEL_PRODUCTS_NEW, Level
from web_interface.templating import templates


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
        source_product = _find_product(source_sku)
        if source_product is not None:
            level = Level.child(
                _product_level(source_product),
                "Copy",
                f"/products/{source_sku}/copy",
            )
        else:
            # Source product vanished (race with a concurrent delete)
            # between the GET that rendered this form and this re-render
            # — fall back rather than crash; there is no product level to
            # nest under.
            level = Level.child(LEVEL_PRODUCTS, "Copy", f"/products/{source_sku}/copy")
    else:
        level = LEVEL_PRODUCTS_NEW
    return templates.TemplateResponse(
        request,
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
