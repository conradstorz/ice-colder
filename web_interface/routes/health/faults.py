"""The /health/faults list and its two-tap Clear flow."""

import hashlib
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ice_contracts.board.vending_machine import (
    PAYMENT_BLOCKING_FAULTS,
    FaultCode,
)
from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.levels import (
    LEVEL_HEALTH_FAULTS,
)
from ice_ui.templating import templates

router = APIRouter()


# active_faults() reports fault codes as strings (FaultCode.value); compare
# against the contract's enum set once, here, rather than in fault_gate.
_PAYMENT_BLOCKING_CODES = {code.value for code in PAYMENT_BLOCKING_FAULTS}

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


def fault_gate(fault: dict) -> str:
    """Classify one active fault into "safety" / "fulfillment" / "alert".

    Executor resolution 1 (task-6-brief.md): the spec asks for "the gate
    class per fault", but no such mapping exists in the codebase --
    services/availability.py's Gate enum classifies permissive *rows*, not
    faults, and contracts/vending_machine.py's FAULT_TABLE carries
    severity/scope but no gate. This derives it once, here, and nowhere
    else (never inline in a template, never re-derived):

      * code in PAYMENT_BLOCKING_FAULTS -> "safety" (those six codes are
        exactly the ones that can inhibit payment -- see
        services/availability.py's Gate docstring)
      * else scope == "product" -> "fulfillment" (blocks only that sale)
      * else -> "alert" (blocks nothing)

    A reversible part-2 stand-in: the spec never defined a fault-to-gate
    mapping: a future task may add a real one (e.g. a `gate` field on
    FaultSpec) and delete this function.
    """
    if fault["code"] in _PAYMENT_BLOCKING_CODES:
        return "safety"
    if fault["scope"] == "product":
        return "fulfillment"
    return "alert"


def _faults_with_age() -> list[dict]:
    """vmc.active_faults() joined with health_monitor's since_seconds and
    this module's derived gate, on `key` -- exactly the join
    context._render_status already does for the same reason (executor
    resolution 6). A fault renders without an age when no health monitor
    is wired, rather than failing; an empty list when no VMC is wired."""
    if not context.vmc_instance:
        return []
    faults = context.vmc_instance.active_faults()
    ages: dict[str, float | None] = {}
    if context.health_monitor:
        ages = {
            f["key"]: f["since_seconds"]
            for f in context.health_monitor.get_summary()["active_faults"]
        }
    for f in faults:
        f["since_seconds"] = ages.get(f["key"])
        f["gate"] = fault_gate(f)
        f["dom_key"] = dom_safe_key(f["key"])
        # Task 14: only the machine-scope PAY-104 fault ever carries a
        # pending sale; every other fault (product-scope, or another
        # machine-scope code) gets None, keeping its plain Clear button.
        f["pending_sale"] = (
            context.machine_instance.pending_sale_for_recovery()
            if f["code"] == FaultCode.PAY_104.value
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
