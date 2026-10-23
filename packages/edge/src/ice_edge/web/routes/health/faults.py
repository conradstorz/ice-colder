"""The /health/faults list and its two-tap Clear flow."""

import hashlib
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ice_contracts.board.vending_machine import FaultCode
from ice_edge.access.access import Permission
from ice_edge.views.health import faults_with_age
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import (
    LEVEL_HEALTH_FAULTS,
)
from ice_ui.templating import templates

router = APIRouter()


# A fault's `key` is either a FaultCode.value (machine-scope, always drawn
# from this fixed charset) or a product SKU (free text). Only characters in
# this set are safe to drop, unescaped, into a CSS id selector.
_SELECTOR_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def dom_safe_key(key: str) -> str:
    """A selector-safe id fragment for a fault's #clear-<key> button.

    Copilot review (PR 20, comment 4113241371): for a product-scoped
    fault, `key` is the SKU, which is free text and not guaranteed to be
    CSS-selector-safe -- a SKU containing "." makes an unescaped
    "#clear-<sku>" selector parse the "." as a class-selector delimiter,
    so it resolves to the wrong element (or none), and the two-tap Clear
    control can't swap its own confirmation state or target the clear
    response.

    Keys already made only of selector-safe characters pass through
    unchanged -- existing ids such as "clear-PAY-103" (a machine
    FaultCode.value, always drawn from a fixed safe charset) must not
    change; tests/test_routes_health.py asserts on that literal string.
    Anything else is replaced by a stable hash of the raw key, so two
    different unsafe keys can never collide with each other or with an
    unrelated safe key. The raw key itself is untouched everywhere else
    (URLs, active-fault lookups) -- only this derived id changes."""
    if _SELECTOR_SAFE_KEY_RE.fullmatch(key):
        return key
    return "h" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _faults_with_age() -> list[dict]:
    """The shared join (`ice_edge.views.health.faults_with_age`) plus this
    page's selector-safe `dom_key`."""
    faults = faults_with_age(
        context.vmc_instance,
        context.health_monitor,
        lambda: context.machine_instance.pending_sale_for_recovery(),
    )
    for f in faults:
        f["dom_key"] = dom_safe_key(f["key"])
        # Only PAY-104 carries snapshot detail (leftover credit etc.).
        f["recovery_detail"] = (
            context.machine_instance.recovery_detail()
            if f["code"] == "PAY-104" and context.machine_instance
            else None
        )
    return faults


def _render_fault_list_oob(request: Request) -> HTMLResponse:
    """Re-render health_faults.html's `body` block standalone, marked
    hx-swap-oob, for POST /health/faults/{key}/clear's response
    (executor resolution 9).

    confirm_button.html's Confirm tap has a fixed hx-target: the
    just-cleared fault's own small per-row button wrapper
    (#clear-<key>) -- but clearing a fault must remove its whole row,
    not just change that one button's state, so the entire
    #fault-list container is refreshed out-of-band instead, the same
    pattern base.html's own #bar already relies on (Task 4) for
    exactly the same reason: something outside the literal hx-target
    needs to change too.

    Template.new_context() + Template.blocks["body"] is the standard
    Jinja2 way to render one block without going through the
    {% extends %} chain (confirmed against this project's Jinja2
    3.1.6), so this reuses health_faults.html's own row markup as the
    single source of truth rather than duplicating it in Python.
    """
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    can_clear = Permission.clear_faults in perms
    faults = _faults_with_age()
    template = templates.get_template("health_faults.html")
    ctx = template.new_context(
        context.template_context(
            request,
            level=LEVEL_HEALTH_FAULTS,
            faults=faults,
            can_clear=can_clear,
            oob=True,
        )
    )
    html = "".join(template.blocks["body"](ctx))
    return HTMLResponse(html)


@router.get(
    "/health/faults",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.view_status))],
)
async def faults_view(request: Request):
    principal = web_auth.current_principal(request)
    perms = principal.perms if principal else frozenset()
    can_clear = Permission.clear_faults in perms
    return templates.TemplateResponse(
        request,
        "health_faults.html",
        context.template_context(
            request,
            level=LEVEL_HEALTH_FAULTS,
            faults=_faults_with_age(),
            can_clear=can_clear,
            oob=False,
        ),
    )


@router.get(
    "/health/faults/{key}/clear/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.clear_faults))],
)
async def fault_clear_confirm(
    request: Request, key: str, confirming: str | None = Query(default=None)
):
    """confirm_button.html's confirm_url contract (Task 5): absent or
    anything but the literal string "false" renders the confirming
    (Confirm/Cancel) state; "false" renders the plain first-tap
    button -- this is what its Cancel button sends via hx-vals."""
    return templates.TemplateResponse(
        request,
        "partials/confirm_button.html",
        context.template_context(
            request,
            label="Clear",
            confirm_label="Confirm",
            post_url=f"/health/faults/{key}/clear",
            target=f"#clear-{dom_safe_key(key)}",
            confirm_url=f"/health/faults/{key}/clear/confirm",
            confirming=(confirming != "false"),
        ),
    )


@router.post(
    "/health/faults/{key}/clear",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.clear_faults)),
        Depends(context.require_htmx),
    ],
)
async def clear_fault(request: Request, key: str):
    """Replaces the old POST /faults/{key}/clear (legacy.py keeps that
    route for Home's status fragment; Task 15 retires it -- executor
    resolution 2).

    Copilot review (PR 21): health_faults.html already hides this
    plain Clear button in favor of the two Task 14 recovery actions
    (Record sale / Discard) whenever PAY-104 carries a pending sale --
    but that is a UI-only guard, and this route is reachable directly
    (a stale confirm URL, curl, devtools) regardless of what the
    template rendered. `clear_fault` on PAY-104 removes the session
    evidence file with no record of and no explicit decision about the
    pending sale, silently losing the only account of that money. So
    this rejects a plain Clear on PAY-104 the same way the server
    already refuses other bypassed-UI-guard writes elsewhere in this
    app, leaving `/PAY-104/record-sale` and `/PAY-104/discard` as the
    only way to resolve it.
    """
    vmc = context.vmc_instance
    machine = context.machine_instance
    if not vmc:
        raise HTTPException(status_code=404, detail=f"No active fault with key {key}")
    if (
        key == FaultCode.PAY_104.value
        and machine.pending_sale_for_recovery() is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "PAY-104 has a pending sale on record -- use Record "
                "sale or Discard instead of Clear, so the money is "
                "accounted for one way or the other rather than "
                "silently dropped."
            ),
        )
    if key == FaultCode.SVC_102.value and machine.maintenance_hold is not None:
        # Copilot review (PR 22): sibling of the PAY-104 guard above --
        # a generic Clear must not bypass the maintenance lease
        # invariant. VMC.clear_fault also refuses this (defense in
        # depth for any other caller), but this route raises the more
        # informative 409 rather than surfacing that refusal as a
        # misleading 404 "no active fault".
        raise HTTPException(
            status_code=409,
            detail=(
                "SVC-102 is held by an active maintenance lease -- "
                "use the Tests level's End/Take over instead of Clear, "
                "so payment cannot be silently re-enabled mid-test."
            ),
        )
    if not vmc.clear_fault(key, by="admin"):
        raise HTTPException(status_code=404, detail=f"No active fault with key {key}")
    return _render_fault_list_oob(request)
