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
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import AccessError, OwnerExistsError, Permission, Role
from services.auth_policy import pin_problem
from services.config_store import (
    add_product,
    delete_product,
    save_config,
    update_product,
)
from services.fsm_control import perform_command
from services.health_monitor import HealthMonitor
from services.mailer import send_email
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
        "/logs",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_logs))],
    )
    async def view_logs(request: Request):
        lines = await asyncio.to_thread(context.tail, context.LOG_PATH, 10)
        return templates.TemplateResponse(
            "partials/logs_fragment.html",
            context.template_context(request, logs=lines),
        )

    @router.get(
        "/health",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def health_summary(request: Request):
        if not context.health_monitor:
            return HTMLResponse("<div>Health monitor not initialized</div>")
        health = context.health_monitor.get_summary()
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        health["availability"] = (
            context.availability.table() if context.availability else []
        )
        health["payment_enabled"] = (
            context.availability.payment_enabled if context.availability else None
        )
        return templates.TemplateResponse(
            "partials/health_fragment.html",
            context.template_context(request, health=health),
        )

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
        "/inventory",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def inventory_view(request: Request):
        return _render_inventory_table(request)

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

    def _guard_owner_target(principal: web_auth.Principal, user_id: str) -> None:
        """403 when *user_id* is the owner and the caller lacks manage_ownership.

        A secretary is the owner's delegate — manage_users lets them touch
        every other user, but the spec (§4) carves the owner out of that:
        any write whose target is the owner needs manage_ownership. Called
        first, before any store mutation, by every one of the four writes
        that take a user id (disable, enable, reset-pin, delete).
        """
        owner = context.access_store.owner()
        if (
            owner is not None
            and owner.id == user_id
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

    def _guard_owner_self_lockout(principal: web_auth.Principal, user_id: str) -> None:
        """403 when *user_id* is the owner disabling or deleting themselves.

        Copilot review, web_interface/routes.py:1277: _guard_owner_target
        only blocks a *non-owner* from targeting the owner; the owner always
        holds manage_ownership, so it let the owner disable or delete their
        own account. Deleting the sole owner leaves setup_finalized true
        with no owner, and ensure_setup_mode()'s begin_setup() then raises
        AccessError("setup has already been finalized") instead of
        recovering — verified directly by
        test_access.py::TestSetupCode::test_begin_setup_after_finalize_raises_and_stays_finalized,
        which already covers exactly this "finalized, no fresh code" state
        with no try/except anywhere on the request path (access_gate,
        setup_page, ensure_setup_mode), so the dashboard would serve a 500
        on every route until someone deletes data/access.json by hand.
        Disabling self is a milder version of the same lockout. Called only
        by disable and delete, in addition to _guard_owner_target;
        reset-pin and enable carry no such risk and are unaffected.
        """
        owner = context.access_store.owner()
        if owner is not None and owner.id == user_id and principal.user.id == user_id:
            raise HTTPException(
                status_code=403, detail="The owner cannot do this to their own account"
            )

    def _guard_owner_device(principal: web_auth.Principal, device) -> None:
        """403 when *device* trusts the owner and the caller lacks
        manage_ownership.

        Sibling to _guard_owner_target for the device case: same
        permission logic (manage_users covers everyone except the owner,
        manage_ownership is required to touch the owner), but the target
        here is "a device the owner is trusted on" rather than "the owner's
        user record" — spec §4's "remove devices" line sits inside the
        manage_users row whose secretary cell reads "never the owner", and
        that carve-out has to reach both device writes (forget, shared) the
        same way it reaches the four user writes. Called after the caller
        has already confirmed *device* exists (a 404 for an unknown id must
        not depend on ownership), before any store mutation.
        """
        owner = context.access_store.owner()
        if (
            owner is not None
            and owner.id in device.trusted_user_ids
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

    def _user_row(user) -> dict:
        # A plain dict with only what a template needs — never pin_hash or
        # pin_salt, the same hazard web_auth.TemplateUser exists to avoid
        # for current_user (see its docstring).
        return {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role.value,
            "disabled": user.disabled,
            "last_login_at": user.last_login_at,
        }

    def _render_users_list(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        transfer_code: str | None = None,
        new_codes: list[str] | None = None,
    ):
        owner = context.access_store.owner()
        users = sorted(context.access_store.users.values(), key=lambda u: u.name)
        device_counts = {
            u.id: sum(
                1
                for d in context.access_store.devices.values()
                if u.id in d.trusted_user_ids
            )
            for u in users
        }
        # This partial is the render target for /users/transfer and
        # /users/codes/regenerate, which pass transfer_code/new_codes —
        # plaintext secrets shown exactly once (Copilot review,
        # web_interface/routes.py:647, extended per its own note to "any
        # response that renders plaintext codes"). no-store unconditionally
        # rather than only when those are set, so the same header applies
        # every time this function is the response, not just sometimes.
        resp_headers = {"Cache-Control": "no-store", **(headers or {})}
        return templates.TemplateResponse(
            "partials/users_list.html",
            context.template_context(
                request,
                users=[_user_row(u) for u in users],
                owner_id=owner.id if owner else None,
                device_counts=device_counts,
                unused_codes=context.access_store.unused_emergency_code_count(),
                pending_transfer=context.access_store.pending_transfer,
                error=error,
                notice=notice,
                # Each shown exactly once, in the response that created it —
                # never persisted, never re-rendered on a later request.
                transfer_code=transfer_code,
                new_codes=new_codes,
            ),
            status_code=status_code,
            headers=resp_headers,
        )

    @router.get(
        "/users",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def users_list(request: Request):
        return _render_users_list(request)

    def _new_user_form(
        request: Request,
        principal: web_auth.Principal,
        *,
        error: str | None = None,
        form: dict | None = None,
    ):
        # A secretary may create every role except owner — there is exactly
        # one owner, and only manage_ownership can mint one (spec §4.1).
        roles = [
            r
            for r in Role
            if r is not Role.owner or Permission.manage_ownership in principal.perms
        ]
        return templates.TemplateResponse(
            "partials/user_form.html",
            context.template_context(
                request,
                roles=roles,
                error=error,
                form=form or {"name": "", "email": "", "role": ""},
            ),
        )

    @router.get(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def new_user_form(request: Request):
        principal = web_auth.current_principal(request)
        return _new_user_form(request, principal)

    @router.post(
        "/users/new",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def create_user(
        request: Request,
        name: str = Form(...),
        email: str = Form(""),
        role: str = Form(...),
        pin: str = Form(...),
    ):
        principal = web_auth.current_principal(request)
        try:
            role_enum = Role(role)
        except ValueError:
            return _render_users_list(request, error="Invalid role.")
        # A secretary submitting role=owner directly (bypassing the hidden
        # <option>) must still be refused server-side — the form only hides
        # the control, it is never the authority.
        if (
            role_enum is Role.owner
            and Permission.manage_ownership not in principal.perms
        ):
            raise HTTPException(status_code=403, detail="Not permitted")

        problem = pin_problem(pin)
        if problem:
            # Spec §6: a PIN that fails pin_problem returns with the reason
            # and creates nobody — the list is the surface this renders
            # back into, so that is what carries the error here.
            return _render_users_list(request, error=problem)

        try:
            context.access_store.create_user(name, email or None, role_enum, pin)
        except OwnerExistsError:
            return _render_users_list(
                request, error="This machine already has an owner."
            )
        return _render_users_list(request, notice=f"{name} added.")

    @router.post(
        "/users/{user_id}/disable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def disable_user(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            context.access_store.set_user_disabled(user_id, True)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        # A disabled user must not keep an open tab working until it idles
        # out on its own (spec's intent behind disable existing at all).
        context.access_store.end_sessions_for_user(user_id)
        return _render_users_list(request)

    @router.post(
        "/users/{user_id}/enable",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def enable_user(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        try:
            context.access_store.set_user_disabled(user_id, False)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        return _render_users_list(request)

    @router.post(
        "/users/{user_id}/reset-pin",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def reset_user_pin(request: Request, user_id: str, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        problem = pin_problem(pin)
        if problem:
            return _render_users_list(request, error=problem)
        try:
            # set_user_pin rehashes and drops the user from every device, so
            # the next login re-enrolls (spec §4.1) — AccessStore already
            # does both halves of that.
            context.access_store.set_user_pin(user_id, pin)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        # set_user_pin only drops device *trust*; resolve_session() does not
        # re-check it, so a session opened before the reset would otherwise
        # keep working on its old device until it idles out on its own —
        # disable and delete already end sessions on their own writes, this
        # one didn't (Copilot review, web_interface/routes.py:1493).
        context.access_store.end_sessions_for_user(user_id)
        return _render_users_list(request, notice="PIN reset.")

    @router.post(
        "/users/{user_id}/delete",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def delete_user_route(request: Request, user_id: str):
        principal = web_auth.current_principal(request)
        _guard_owner_target(principal, user_id)
        _guard_owner_self_lockout(principal, user_id)
        try:
            context.access_store.delete_user(user_id)
        except AccessError:
            raise HTTPException(status_code=404, detail="No such user")
        context.access_store.end_sessions_for_user(user_id)
        return _render_users_list(request)

    def _check_owner_pin(
        request: Request, principal: web_auth.Principal, pin: str
    ) -> tuple[str, int | None] | None:
        """Verify the caller's own PIN; None on success, else (message,
        retry_after_seconds) on failure.

        `retry_after_seconds` is the whole-second wait to report as a
        Retry-After header (and a 429 status, at the call site) when the
        PIN back-off has tripped; it is None for an ordinary wrong-PIN
        refusal, which stays a plain re-render — the shape every other
        back-off site in this file already uses (see /login's PIN check,
        /login/enroll/send and /login/enroll, and /setup): a bare error
        message cannot carry a status code, so a caller that only got a
        string would fall through to the route's default 200.

        Always the caller's own user id — never some other user's — under
        back-off kind "pin", the same kind and subject a login attempt for
        this user uses, so a stranger burning down this budget on the
        login page also slows an attacker here and vice versa. `trusted`
        follows the caller's own device, exactly as login does.
        """
        client = web_auth.client_key(request)
        trusted = web_auth.is_trusted_client(request, principal.user.id)
        remaining = web_auth.backoff.check(
            "pin", principal.user.id, client, trusted=trusted
        )
        if remaining is not None:
            retry_after = int(remaining) + 1
            return f"Too many attempts. Try again in {retry_after} s.", retry_after
        if not context.access_store.verify_user_pin(principal.user.id, pin):
            web_auth.backoff.record_failure(
                "pin", principal.user.id, client, trusted=trusted
            )
            return "Wrong PIN.", None
        web_auth.backoff.record_success(
            "pin", principal.user.id, client, trusted=trusted
        )
        return None

    def _owner_pin_problem_response(request: Request, problem: tuple[str, int | None]):
        """Render the users list for a _check_owner_pin failure.

        A tripped back-off (retry_after is not None) answers 429 with
        Retry-After, matching every other back-off site in this file; an
        ordinary wrong PIN stays a plain 200 re-render with the error.
        """
        message, retry_after = problem
        if retry_after is not None:
            return _render_users_list(
                request,
                error=message,
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return _render_users_list(request, error=message)

    @router.post(
        "/users/transfer",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(context.require_htmx),
        ],
    )
    async def start_ownership_transfer(
        request: Request, pin: str = Form(...), emergency_code: str = Form(...)
    ):
        # Guard first, before the PIN check and before the code check, so a
        # re-entrant call while a transfer is already pending can spend
        # nothing: it must not consume a second emergency code, must not
        # overwrite the existing pending_transfer, and must not invalidate
        # the transfer code already handed to the incoming owner. The
        # template hides this form while a transfer is pending, but that is
        # not the authority (spec §4) — this check is.
        if context.access_store.pending_transfer is not None:
            return _render_users_list(
                request,
                error=(
                    "A transfer is already pending. Cancel it before starting another."
                ),
            )

        # Spec §3.3 step 1's whole point: check first, consume only on
        # success, never the other way round — a wrong PIN must leave the
        # emergency-code pool untouched, or a typo in the owner's own
        # browser could burn down the machine's only offline recovery.
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)

        client = web_auth.client_key(request)
        remaining = web_auth.backoff.check("transfer", "pool", client)
        if remaining is not None:
            return _render_users_list(
                request,
                error=f"Too many attempts. Try again in {int(remaining) + 1} s.",
                status_code=429,
            )

        # consume_emergency_code only mutates the pool on a match, so a
        # wrong code both fails this check and consumes nothing — the PIN
        # check above already ran, so this is the only mutation gated on
        # both proofs passing.
        if not context.access_store.consume_emergency_code(
            emergency_code.strip(), principal.user.id, "transfer"
        ):
            web_auth.backoff.record_failure("transfer", "pool", client)
            return _render_users_list(request, error="That code was not accepted.")
        web_auth.backoff.record_success("transfer", "pool", client)

        # Nothing else changes here: the current owner stays fully in
        # control until the incoming owner completes the wizard.
        transfer_code = context.access_store.start_transfer(principal.user.id)
        return _render_users_list(
            request,
            notice="Ownership transfer started. Give this code to the new owner.",
            transfer_code=transfer_code,
        )

    @router.post(
        "/users/transfer/cancel",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(context.require_htmx),
        ],
    )
    async def cancel_ownership_transfer(request: Request, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)
        context.access_store.cancel_transfer()
        return _render_users_list(request, notice="Ownership transfer cancelled.")

    @router.post(
        "/users/codes/regenerate",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(context.require_htmx),
        ],
    )
    async def regenerate_emergency_codes_route(request: Request, pin: str = Form(...)):
        principal = web_auth.current_principal(request)
        problem = _check_owner_pin(request, principal, pin)
        if problem:
            return _owner_pin_problem_response(request, problem)
        # Replaces the whole pool, used codes included; the old codes stop
        # working immediately (AccessStore.generate_emergency_codes).
        new_codes = context.access_store.generate_emergency_codes()
        return _render_users_list(
            request, notice="Emergency codes regenerated.", new_codes=new_codes
        )

    @router.post(
        "/users/report",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_ownership)),
            Depends(context.require_htmx),
        ],
    )
    async def email_machine_report_route(request: Request):
        principal = web_auth.current_principal(request)
        owner = principal.user
        if not context._can_email_owner(owner):
            return _render_users_list(
                request,
                error="Email is not available; check the email gateway settings.",
            )
        gateway = context.config.communication.email_gateway
        # machine_report already omits every hash and PIN (spec §3.4).
        report = context.access_store.machine_report(context.config)
        ok = await send_email(
            gateway, owner.email, "Vending machine access report", report
        )
        if not ok:
            return _render_users_list(request, error="Email could not be sent.")
        return _render_users_list(request, notice=f"Report emailed to {owner.email}.")

    def _device_row(device) -> dict:
        # A plain dict with only what the template needs — never token_hash,
        # the fingerprint of the cookie value has no business on a page.
        return {
            "id": device.id,
            "label": device.label,
            "shared": device.shared,
            "trusted_user_ids": list(device.trusted_user_ids),
            "last_seen_at": device.last_seen_at,
        }

    def _render_devices_list(request: Request, *, notice: str | None = None):
        owner = context.access_store.owner()
        devices = sorted(context.access_store.devices.values(), key=lambda d: d.label)
        user_names = {u.id: u.name for u in context.access_store.users.values()}
        return templates.TemplateResponse(
            "partials/devices_list.html",
            context.template_context(
                request,
                devices=[_device_row(d) for d in devices],
                user_names=user_names,
                owner_id=owner.id if owner else None,
                notice=notice,
            ),
        )

    @router.get(
        "/devices",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.manage_users))],
    )
    async def devices_list(request: Request):
        return _render_devices_list(request)

    @router.post(
        "/devices/{device_id}/forget",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def forget_device_route(request: Request, device_id: str):
        device = context.access_store.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        # Existence is checked first so a 404 for an unknown id never
        # depends on ownership; the owner-device guard runs only once we
        # know there is a device to reason about.
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        # Order matters: a forgotten device must not keep whoever is using it
        # logged in one request longer than necessary (resolve_session would
        # eventually refuse it once the device record is gone, but ending the
        # session here is immediate and keeps the in-memory table from
        # growing with sessions nothing will ever resolve again).
        context.access_store.end_sessions_for_device(device_id)
        context.access_store.forget_device(device_id)
        return _render_devices_list(request, notice="Device forgotten.")

    @router.post(
        "/devices/{device_id}/shared",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.manage_users)),
            Depends(context.require_htmx),
        ],
    )
    async def toggle_device_shared_route(request: Request, device_id: str):
        device = context.access_store.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        principal = web_auth.current_principal(request)
        _guard_owner_device(principal, device)
        context.access_store.set_device_shared(device_id, not device.shared)
        return _render_devices_list(request)

    return router
