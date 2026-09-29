"""The six Health levels (spec §2): the /health landing page and its four
sub-levels (Subsystems, Faults, Availability, Logs), plus the per-subsystem
detail page and the fault-clear flow.

Data sources are exactly the ones the old routes/legacy.py `GET /health`
fragment used (health_monitor.get_summary(), HealthMonitor.empty_subsystem_
row(), availability.table()/.payment_enabled/.payment_blocking_reasons(),
vmc.active_faults()) -- this module only splits that single fragment's
content across the six levels; see .superpowers/sdd/part2/task-6-brief.md.
"""

import asyncio
import hashlib
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from contracts.vending_machine import (
    EXPECTED_SUBSYSTEMS,
    PAYMENT_BLOCKING_FAULTS,
    FaultCode,
)
from services.access import Permission
from services.health_monitor import HealthMonitor
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import (
    LEVEL_HEALTH,
    LEVEL_HEALTH_AVAILABILITY,
    LEVEL_HEALTH_FAULTS,
    LEVEL_HEALTH_LOGS,
    LEVEL_HEALTH_SUBSYSTEMS,
    Level,
)

# active_faults() reports fault codes as strings (FaultCode.value); compare
# against the contract's enum set once, here, rather than in _fault_gate.
_PAYMENT_BLOCKING_CODES = {code.value for code in PAYMENT_BLOCKING_FAULTS}

# Task 14 review finding 1: pending_sale_for_recovery()'s read, record_sale's
# write and clear_fault's clear must run as one critical section, or a
# second request arriving while the first is mid-write (record_sale runs on
# a worker thread via asyncio.to_thread, which yields the event loop for the
# duration) finds the fault still active and the snapshot still on disk and
# repeats the whole sequence -- a second row for one sale. A module-level
# asyncio.Lock, held for the full check-write-clear span of both
# /record-sale and /discard, closes that: the two routes below never run
# their bodies concurrently with each other or with themselves.
#
# This serialises only within one process. main.py runs a single in-process
# uvicorn server (one event loop, no worker-process pool), so a process-wide
# lock is sufficient for this deployment -- it would not be if uvicorn were
# ever run with multiple workers (each worker has its own Python process and
# therefore its own, independent lock instance).
_pay104_lock = asyncio.Lock()

# A fault's `key` is either a FaultCode.value (machine-scope, always drawn
# from this fixed charset) or a product SKU (free text). Only characters in
# this set are safe to drop, unescaped, into a CSS id selector.
_SELECTOR_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _dom_safe_key(key: str) -> str:
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


def _fault_gate(fault: dict) -> str:
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


def _subsystem_summary() -> dict[str, dict]:
    """One row per EXPECTED_SUBSYSTEMS entry: the live row from the health
    monitor when it has ever heard from that subsystem, else the neutral
    empty_subsystem_row() placeholder -- and the placeholder for every
    subsystem when no health monitor is wired at all (rule 3)."""
    live = (
        context.health_monitor.get_summary()["subsystems"]
        if context.health_monitor
        else {}
    )
    return {
        name: live.get(name, HealthMonitor.empty_subsystem_row())
        for name in EXPECTED_SUBSYSTEMS
    }


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
        f["gate"] = _fault_gate(f)
        f["dom_key"] = _dom_safe_key(f["key"])
        # Task 14: only the machine-scope PAY-104 fault ever carries a
        # pending sale; every other fault (product-scope, or another
        # machine-scope code) gets None, keeping its plain Clear button.
        f["pending_sale"] = (
            context.vmc_instance.pending_sale_for_recovery()
            if f["code"] == FaultCode.PAY_104.value
            else None
        )
    return faults


def _availability_context_line(avail) -> str:
    """Availability tile's summary line on /health (resolution 8)."""
    if not avail:
        return "—"
    if avail.payment_enabled:
        return "Payment enabled"
    n = len(avail.payment_blocking_reasons())
    return f"Payment disabled ({n})"


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

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
        "/health",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def health_home(request: Request):
        principal = web_auth.current_principal(request)
        perms = principal.perms if principal else frozenset()
        summary = (
            context.health_monitor.get_summary() if context.health_monitor else None
        )
        subsystems = _subsystem_summary()
        faults = _faults_with_age()
        ok = sum(1 for r in subsystems.values() if r["alive"] and not r["stale"])

        # Resolution 8: build all four sub-tile dicts with the keys
        # partials/tile.html expects, coming_soon false on all four --
        # Logs is the only one whose `enabled` depends on the viewer's
        # permissions rather than always being true.
        tiles = [
            {
                "title": "Subsystems",
                "url": LEVEL_HEALTH_SUBSYSTEMS.url,
                "icon": "health",
                "context": f"{ok}/{len(subsystems)} OK",
                "enabled": True,
                "coming_soon": False,
            },
            {
                "title": "Faults",
                "url": LEVEL_HEALTH_FAULTS.url,
                "icon": "health",
                "context": f"{len(faults)} active fault{'s' if len(faults) != 1 else ''}",
                "enabled": True,
                "coming_soon": False,
            },
            {
                "title": "Availability",
                "url": LEVEL_HEALTH_AVAILABILITY.url,
                "icon": "health",
                "context": _availability_context_line(context.availability),
                "enabled": True,
                "coming_soon": False,
            },
            {
                "title": "Logs",
                "url": LEVEL_HEALTH_LOGS.url,
                "icon": "health",
                "context": "Last 50 lines",
                "enabled": Permission.view_logs in perms,
                "coming_soon": False,
            },
        ]

        return templates.TemplateResponse(
            "health.html",
            context.template_context(
                request,
                level=LEVEL_HEALTH,
                tiles=tiles,
                mqtt_connected=summary["mqtt_connected"] if summary else None,
                vmc_state=summary["vmc_state"] if summary else None,
                vmc_build=summary["vmc"] if summary else None,
            ),
        )

    @router.get(
        "/health/subsystems",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def subsystems_view(request: Request):
        return templates.TemplateResponse(
            "health_subsystems.html",
            context.template_context(
                request, level=LEVEL_HEALTH_SUBSYSTEMS, subsystems=_subsystem_summary()
            ),
        )

    @router.get(
        "/health/subsystems/{name}",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def subsystem_detail(request: Request, name: str):
        if name not in EXPECTED_SUBSYSTEMS:
            raise HTTPException(status_code=404, detail=f"Unknown subsystem '{name}'")
        row = _subsystem_summary()[name]

        # health_monitor.get_summary()["temperatures"] is keyed by sensor
        # location (e.g. "evaporator", "cabinet"), with no field anywhere
        # linking a location back to the EXPECTED_SUBSYSTEMS name that
        # reported it -- the spec asks for "temperature ranges where the
        # subsystem reports them", but there is no subsystem -> location
        # mapping in the data model to filter by, and adding one is out of
        # this task's scope (services/health_monitor.py is not ours to
        # touch). Reversible part-2 stand-in: show every known reading on
        # every subsystem's detail page rather than fabricate an
        # attribution the codebase doesn't support.
        summary = (
            context.health_monitor.get_summary() if context.health_monitor else None
        )
        temperatures = summary["temperatures"] if summary else {}

        level = Level.child(LEVEL_HEALTH_SUBSYSTEMS, name, f"/health/subsystems/{name}")
        return templates.TemplateResponse(
            "health_subsystem.html",
            context.template_context(
                request, level=level, name=name, row=row, temperatures=temperatures
            ),
        )

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
            "partials/confirm_button.html",
            context.template_context(
                request,
                label="Clear",
                confirm_label="Confirm",
                post_url=f"/health/faults/{key}/clear",
                target=f"#clear-{_dom_safe_key(key)}",
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
        if not vmc:
            raise HTTPException(
                status_code=404, detail=f"No active fault with key {key}"
            )
        if (
            key == FaultCode.PAY_104.value
            and vmc.pending_sale_for_recovery() is not None
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
        if key == FaultCode.SVC_102.value and vmc.maintenance_hold is not None:
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
            raise HTTPException(
                status_code=404, detail=f"No active fault with key {key}"
            )
        return _render_fault_list_oob(request)

    def _pay104_active() -> bool:
        return bool(context.vmc_instance) and any(
            f["code"] == FaultCode.PAY_104.value
            for f in context.vmc_instance.active_faults()
        )

    @router.get(
        "/health/faults/PAY-104/record-sale/confirm",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.clear_faults))],
    )
    async def pay104_record_sale_confirm(
        request: Request, confirming: str | None = Query(default=None)
    ):
        """Same confirm_url contract as fault_clear_confirm above."""
        dom_key = _dom_safe_key(FaultCode.PAY_104.value)
        return templates.TemplateResponse(
            "partials/confirm_button.html",
            context.template_context(
                request,
                label="Record sale",
                confirm_label="Confirm",
                post_url="/health/faults/PAY-104/record-sale",
                target=f"#record-sale-{dom_key}",
                confirm_url="/health/faults/PAY-104/record-sale/confirm",
                confirming=(confirming != "false"),
            ),
        )

    @router.post(
        "/health/faults/PAY-104/record-sale",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.clear_faults)),
            Depends(context.require_htmx),
        ],
    )
    async def pay104_record_sale(request: Request):
        """Write the pending sale through record_sale, then clear PAY-104
        (which discards the snapshot as part of the existing clear path).

        Ordering is write-then-clear, deliberately: record_sale is
        synchronous, durable and already journals the record itself
        (append + fsync) before re-raising on any insert failure (spec
        §1.2) -- so if it raises, the row is never lost, but PAY-104 must
        stay active rather than be cleared with nothing recorded in the
        database, or an operator would have no way to know a retry is
        still owed. Clearing first and writing second would risk exactly
        that: a crash between the two leaves the fault cleared with no row
        and no evidence file to recover from.

        Idempotency: `pending_sale_for_recovery()` returns None once the
        fault has already been cleared (by this route, by `/discard`, or
        by a plain admin Clear) -- the session snapshot is the idempotency
        token, discarded in the same `clear_fault` call that removes the
        fault. A replayed POST (double-tap, retried request, a second
        operator) then finds nothing pending and is treated as already
        handled, not an error.

        The whole check-write-clear sequence runs under `_pay104_lock`
        (review finding 1): without it, a second request arriving while
        `record_sale`'s `asyncio.to_thread` await has yielded the event
        loop -- still inside this same critical section -- would repeat
        the same read of `pending_sale_for_recovery()`, find the fault
        still active and the snapshot still on disk, and write a second
        row for the same sale.

        Review finding 2: a successful write followed by a `clear_fault`
        failure (the snapshot could not be removed) must not leave a
        window where a later request re-records the same sale. On that
        path, `mark_pending_sale_recorded()` durably clears the
        snapshot's pending-sale shares before this returns -- so any
        later call to `pending_sale_for_recovery()` reports `None` (its
        own contract: no shares, no pending sale) even though PAY-104
        legitimately remains active for the operator to acknowledge.

        Review finding 3: `mark_pending_sale_recorded()` can itself fail
        -- it goes through the same `SessionStore` against the same disk
        that just made `clear_fault`'s removal fail one line above, so
        this is a realistic pairing, not a contrived one. When it does,
        `pending_sale_for_recovery()` keeps (truthfully) reporting the
        original sale, since the snapshot was never rewritten.
        `vmc.reserve_pending_sale`/`pending_sale_already_recorded` still
        add an in-memory guard, checked under this same lock, so a retry
        *within this process* short-circuits before `record_sale` is
        called again at all -- but that guard is not what makes a retry
        *safe*, only what makes it cheap. Safety across every retry,
        including one after this process has restarted, comes from the
        database itself (part 3 review, round 4): the call below passes
        `ts=pending["saved_at"]` (the session snapshot's dispense-time
        timestamp, stable across any number of reloads of the same
        snapshot -- see `VMC._snapshot`/`SessionSnapshot.saved_at`) and
        `idempotent=True`, so a second attempt at the same pending sale --
        same process, a different process, after a restart, disk fixed or
        not -- inserts zero rows because `(ts, sku)` is already present.
        `DATA_101` (raised below when the marker write fails) is still
        raised, because a storage problem is real and the operator should
        see it, but it is an alert about that storage problem, not the
        mechanism that prevents a duplicate row -- the earlier round's
        report claimed the in-memory guard's absence after a restart left
        `DATA_101` as "the operator's surviving signal" protecting against
        a double-write; that was wrong (see the corrected report), and is
        moot now regardless: the database's own idempotent insert protects
        every retry, with or without any fault visible on the Faults page.
        """
        vmc = context.vmc_instance
        if vmc is None:
            raise HTTPException(status_code=404, detail="No VMC attached")
        marker_unwritable_detail = (
            "Sale recorded; the PAY-104 evidence snapshot could not be "
            "updated -- retrying is safe (the recovered sale is keyed by "
            "its dispense time, so a repeat write cannot record it twice) "
            "-- resolve the storage problem, then clear PAY-104 manually "
            "once it is fixed"
        )
        async with _pay104_lock:
            pending = vmc.pending_sale_for_recovery()
            if pending is None:
                # Already recorded/discarded/cleared by an earlier request
                # -- nothing to do. Money-safe no-op, not an error.
                return _render_fault_list_oob(request)
            if vmc.pending_sale_already_recorded(pending):
                # The durable marker failed to persist on an earlier
                # request in this process (finding 3) -- the sale is
                # already recorded, so this in-memory short-circuit saves
                # a redundant (harmless, per the idempotent insert below)
                # trip to the database and gives the operator the same
                # message as the request that hit the failure.
                raise HTTPException(status_code=500, detail=marker_unwritable_detail)
            if context.event_recorder is None:
                raise HTTPException(
                    status_code=500,
                    detail="No event recorder attached; cannot record sale",
                )
            try:
                # ts=pending["saved_at"] + idempotent=True (part 3 review,
                # round 4): the session snapshot's dispense-time timestamp
                # is a deterministic key for *this* pending sale (stable
                # across any number of snapshot reloads -- see
                # VMC._snapshot/SessionSnapshot.saved_at), so a second
                # attempt at recording it -- from this process, another
                # process, or after a restart -- inserts zero rows instead
                # of a duplicate. Every other caller of record_sale (the
                # live FSM dispense path) keeps passing neither argument,
                # so a fresh sale is still always a plain, non-deduplicated
                # insert.
                await asyncio.to_thread(
                    context.event_recorder.record_sale,
                    pending["sku"],
                    pending["name"],
                    pending["slot"],
                    pending["price"],
                    pending["methods"],
                    ts=pending["saved_at"],
                    idempotent=True,
                )
            except Exception:
                logger.exception(
                    f"PAY-104 record-sale: record_sale failed for "
                    f"sku={pending['sku']!r}; already journaled as fallback by "
                    "record_sale itself -- leaving PAY-104 active so the "
                    "operator can retry"
                )
                vmc.raise_data_fault(
                    FaultCode.DATA_101,
                    outcome=f"sku={pending['sku']} price=${pending['price']:.2f}",
                )
                raise HTTPException(
                    status_code=500,
                    detail="Could not record the sale; PAY-104 left active for retry",
                ) from None
            if not vmc.clear_fault(FaultCode.PAY_104.value, by="admin"):
                # The sale is recorded and must never be recorded again --
                # reserve it in memory (finding 3) before anything else,
                # still inside the lock, so even if the durable marker
                # below also fails, no later request in this process can
                # find a pending sale here again.
                vmc.reserve_pending_sale(pending)
                if vmc.mark_pending_sale_recorded():
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Sale recorded; PAY-104 needs operator attention "
                            "(the snapshot could not be cleared automatically) "
                            "-- retrying will not record the sale again"
                        ),
                    )
                # The marker itself could not be persisted -- surface it
                # as a fault that outlives this HTTP response (finding 3
                # part (c)) so the storage problem is visible, then tell
                # the operator the truth: unlike earlier rounds claimed,
                # retrying here is safe too (the idempotent insert above
                # is what guarantees that now, not this fault or the
                # in-memory guard).
                vmc.raise_data_fault(
                    FaultCode.DATA_101,
                    outcome=(
                        f"sku={pending['sku']} price=${pending['price']:.2f}; "
                        "PAY-104 recovery marker could not be written"
                    ),
                )
                raise HTTPException(status_code=500, detail=marker_unwritable_detail)
            return _render_fault_list_oob(request)

    @router.get(
        "/health/faults/PAY-104/discard/confirm",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.clear_faults))],
    )
    async def pay104_discard_confirm(
        request: Request, confirming: str | None = Query(default=None)
    ):
        dom_key = _dom_safe_key(FaultCode.PAY_104.value)
        return templates.TemplateResponse(
            "partials/confirm_button.html",
            context.template_context(
                request,
                label="Discard",
                confirm_label="Confirm",
                post_url="/health/faults/PAY-104/discard",
                target=f"#discard-{dom_key}",
                confirm_url="/health/faults/PAY-104/discard/confirm",
                confirming=(confirming != "false"),
            ),
        )

    @router.post(
        "/health/faults/PAY-104/discard",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.clear_faults)),
            Depends(context.require_htmx),
        ],
    )
    async def pay104_discard(request: Request):
        """Clear PAY-104 (discarding the snapshot) without recording a sale.

        Idempotent the same way `/record-sale` is: if PAY-104 is no longer
        active (already discarded, already recorded, or cleared by a plain
        admin Clear), this is a no-op rather than a 404 -- a replay must
        never surface as an error.

        Shares `_pay104_lock` with `/record-sale` (review finding 1) so a
        discard can never interleave with a record-sale that is mid-write
        for the same fault.
        """
        vmc = context.vmc_instance
        if vmc is None:
            raise HTTPException(status_code=404, detail="No VMC attached")
        async with _pay104_lock:
            if not _pay104_active():
                return _render_fault_list_oob(request)
            if not vmc.clear_fault(FaultCode.PAY_104.value, by="admin"):
                raise HTTPException(status_code=500, detail="Could not clear PAY-104")
            return _render_fault_list_oob(request)

    @router.get(
        "/health/availability",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def availability_view(request: Request):
        avail = context.availability
        rows = avail.table() if avail else []
        payment_enabled = avail.payment_enabled if avail else None
        blocking_reasons = avail.payment_blocking_reasons() if avail else []
        per_kind = {
            "ice": avail.sale_available("ice") if avail else (None, []),
            "water": avail.sale_available("water") if avail else (None, []),
        }
        return templates.TemplateResponse(
            "health_availability.html",
            context.template_context(
                request,
                level=LEVEL_HEALTH_AVAILABILITY,
                rows=rows,
                payment_enabled=payment_enabled,
                blocking_reasons=blocking_reasons,
                per_kind=per_kind,
            ),
        )

    @router.get(
        "/health/logs",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_logs))],
    )
    async def logs_view(request: Request):
        # 50 lines per the spec (executor resolution 7); the old fragment's
        # 10 was never the spec value.
        lines = await asyncio.to_thread(context.tail, context.LOG_PATH, 50)
        return templates.TemplateResponse(
            "health_logs.html",
            context.template_context(request, level=LEVEL_HEALTH_LOGS, logs=lines),
        )

    return router
