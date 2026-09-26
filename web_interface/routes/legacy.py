"""Everything not yet re-homed into its own area module: health, config
tabs, inventory/products, users, devices, activity, logs, action, faults.

Moved verbatim from the old web_interface/routes.py's gated `router`
(Task 1 executor resolution 2). Later plan tasks split these out into
routes/health.py, routes/products.py, routes/inventory.py, routes/users.py
etc. one area at a time; do not pre-split them here.
"""

import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from config.config_model import Product
from services.access import Permission
from services.config_store import (
    add_product,
    delete_product,
    save_config,
    update_product,
)
from services.fsm_control import perform_command
from web_interface import auth as web_auth
from web_interface import context


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/inventory/add",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(context.require_htmx),
        ],
    )
    async def add_new_product(
        request: Request,
        sku: str = Form(...),
        name: str = Form(...),
        price: float = Form(...),
        slot: str | None = Form(None),
        kind: str = Form("other"),
    ):
        parsed_slot = int(slot) if slot not in (None, "") else None
        success = add_product(
            context.config, sku, name, price, slot=parsed_slot, kind=kind
        )
        if success and context.inventory_manager:
            context.inventory_manager.add_sku(sku, 0, tracked=False)

        return _render_inventory_table(request)

    @router.get(
        "/config/machine",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def machine_info(request: Request):
        return templates.TemplateResponse(
            "partials/machine_info.html",
            context.template_context(request, details=context.config.physical),
        )

    @router.get(
        "/config/contacts",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_contacts))],
    )
    async def contact_info(request: Request):
        return templates.TemplateResponse(
            "partials/contacts.html",
            context.template_context(request, people=context.config.physical.people),
        )

    @router.get(
        "/config/payments",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def payment_config(request: Request):
        return templates.TemplateResponse(
            "partials/payments.html",
            context.template_context(request, payment=context.config.payment),
        )

    @router.get(
        "/config/comms",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_secrets))],
    )
    async def comms_config(request: Request):
        return templates.TemplateResponse(
            "partials/comms.html",
            context.template_context(request, comm=context.config.communication),
        )

    def _locked_skus() -> dict[str, str]:
        if not context.vmc_instance:
            return {}
        return {
            f["sku"]: f["code"]
            for f in context.vmc_instance.active_faults()
            if f["scope"] == "product"
        }

    @router.post(
        "/faults/{key}/clear",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.clear_faults)),
            Depends(context.require_htmx),
        ],
    )
    async def clear_fault(request: Request, key: str):
        if not context.vmc_instance or not context.vmc_instance.clear_fault(
            key, by="admin"
        ):
            raise HTTPException(
                status_code=404, detail=f"No active fault with key {key}"
            )
        return await context._render_status(templates, request)

    @router.post(
        "/action/{command}",
        dependencies=[
            Depends(web_auth.require(Permission.machine_controls)),
            Depends(context.require_htmx),
        ],
    )
    async def control_action(command: str):
        result = perform_command(command, context.vmc_instance)
        return HTMLResponse(f"<p>{result}</p>")

    @router.get(
        "/activity",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def activity_fragment(request: Request, period: int = Query(default=24)):
        if not context.event_recorder:
            return HTMLResponse(
                '<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">'
                '<p class="text-gray-400 text-sm">Activity data not available yet.</p></div>'
            )
        if period not in (24, 168, 720):
            period = 24
        summary = await asyncio.to_thread(context.event_recorder.get_summary, period)
        average = await asyncio.to_thread(
            context.event_recorder.get_historical_average, period
        )
        return templates.TemplateResponse(
            "partials/activity_fragment.html",
            context.template_context(
                request, period=period, summary=summary, average=average
            ),
        )

    @router.get(
        "/inventory/edit/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def edit_inventory_catalog(request: Request, sku: str):
        product = next((p for p in context.config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_catalog_form.html",
            context.template_context(request, product=product),
        )

    @router.post(
        "/inventory/update/{sku}/catalog",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(context.require_htmx),
        ],
    )
    async def update_inventory_catalog(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        kind: str = Form("other"),
    ):
        # slot=None leaves the stored slot untouched (services/config_store.py
        # update_product) — this endpoint owns name/price/kind only.
        update_product(context.config, sku, name, price, slot=None, kind=kind)

        return _render_inventory_table(request)

    def _inventory_count(product: Product) -> int:
        if context.inventory_manager:
            return context.inventory_manager.get_count(product.sku)
        return product.inventory_count

    def _is_tracked(product: Product) -> bool:
        if context.inventory_manager:
            return context.inventory_manager.is_tracked(product.sku)
        return product.track_inventory

    def _render_inventory_table(request: Request):
        # Counts live in InventoryManager, not on Product (see
        # _inventory_count above) — every render of this partial must read
        # through it so a loader's placement POST is reflected immediately
        # instead of showing the stale/zero value still on Product.
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            context.template_context(
                request,
                products=context.config.products,
                locked=_locked_skus(),
                inventory_counts={
                    p.sku: _inventory_count(p) for p in context.config.products
                },
            ),
        )

    @router.get(
        "/inventory/edit/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_placement))],
    )
    async def edit_inventory_placement(request: Request, sku: str):
        product = next((p for p in context.config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_placement_form.html",
            context.template_context(
                request,
                product=product,
                inventory_count=_inventory_count(product) if product else 0,
                tracked=_is_tracked(product) if product else False,
            ),
        )

    @router.post(
        "/inventory/update/{sku}/placement",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_placement)),
            Depends(context.require_htmx),
        ],
    )
    async def update_inventory_placement(
        request: Request,
        sku: str,
        slot: int = Form(...),
        inventory_count: int = Form(...),
        track_inventory: str | None = Form(None),
    ):
        product = next((p for p in context.config.products if p.sku == sku), None)
        tracked = track_inventory is not None
        if product:
            if inventory_count < 0:
                # Mirror services/config_store.py's slot < 0 guard: reject
                # the whole write and log a warning rather than let a
                # negative stock level silently corrupt InventoryManager —
                # restocking is the loader's job, and this is exactly the
                # workflow that must not be allowed to corrupt data.
                logger.warning(
                    f"Cannot update placement SKU={sku}: inventory_count "
                    f"{inventory_count} is invalid (must be >= 0)"
                )
                return _render_inventory_table(request)

            # This endpoint owns slot/count/tracking only — pass the
            # product's own stored name/price/kind through unchanged so a
            # placement POST can never smuggle a catalog change, regardless
            # of what a hostile form body contains.
            slot_ok = update_product(
                context.config, sku, product.name, product.price, slot=slot, kind=None
            )
            # update_product returns False when the requested slot is
            # already in use (or negative); that used to be ignored, so a
            # rejected slot change still wrote the count/tracking below,
            # leaving placement state half-applied (Copilot review,
            # web_interface/routes.py:1202). A validation failure must leave
            # every field of this form unchanged, not just the slot.
            if not slot_ok:
                return _render_inventory_table(request)
            if context.inventory_manager:
                context.inventory_manager.add_sku(sku, inventory_count, tracked=tracked)
            else:
                product.inventory_count = inventory_count
                product.track_inventory = tracked
                save_config(context.config)

        return _render_inventory_table(request)

    @router.post(
        "/inventory/delete/{sku}",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_catalog)),
            Depends(context.require_htmx),
        ],
    )
    async def delete_inventory_item(request: Request, sku: str):
        success = delete_product(context.config, sku)
        if success and context.inventory_manager:
            context.inventory_manager.remove_sku(sku)
        return _render_inventory_table(request)

    @router.get(
        "/inventory/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def new_product_form(request: Request):
        # Blank form, random temporary SKU
        random_sku = f"SKU-{uuid4().hex[:6].upper()}"
        product = Product(sku=random_sku, name="", price=0.0, inventory_count=0)
        return templates.TemplateResponse(
            "partials/inventory_add_form.html",
            context.template_context(request, product=product, mode="new"),
        )

    @router.get(
        "/inventory/copy/{sku}",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_catalog))],
    )
    async def copy_product_form(request: Request, sku: str):
        base = next((p for p in context.config.products if p.sku == sku), None)
        if base:
            new_sku = f"SKU-{uuid4().hex[:6].upper()}"
            copied = Product(
                sku=new_sku,
                name=f"{base.name} Copy",
                price=base.price,
                inventory_count=base.inventory_count,
                description=base.description,
                image_url=base.image_url,
                track_inventory=base.track_inventory,
                kind=base.kind,
            )
            return templates.TemplateResponse(
                "partials/inventory_add_form.html",
                context.template_context(request, product=copied, mode="copy"),
            )

    return router
