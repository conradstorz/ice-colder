"""The Inventory restock level (spec §2 "Inventory row"): GET /inventory
and POST /inventory/{sku}/adjust — the screen a loader uses while standing
at the open machine with a box of stock, tapping counts up and down.

Deletes GET /inventory's old coverage in routes/legacy.py's
`inventory_view` (same commit — task-8 brief resolution 1): FastAPI
matches the first registered route, and web_interface/routes/__init__.py
includes legacy.build_router() before this module's, so leaving that old
handler in place would silently shadow this level's GET /inventory.
Every other /inventory/* route in legacy.py (add, new, copy, the
catalog/placement edit+update forms, delete) is untouched here; Task 15
retires the rest.

Counts live in InventoryManager, never in the stale Product.inventory_count
seed field (part 1's d58da37, brief resolution 4) — see _row_for below.

Task 11 adds the cash collection endpoints: GET /inventory/collect/confirm
and POST /inventory/collect with a two-tap confirm using
partials/confirm_button.html.
"""

import asyncio
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from web_interface.levels import LEVEL_INVENTORY

# The only four deltas the four adjust buttons ever send (brief resolution
# 6). The endpoint is reachable directly, not only from those buttons, so
# anything else is refused with 400 rather than trusted from the form.
_ALLOWED_DELTAS = frozenset({-10, -1, 1, 10})

# Serializes the enqueue -> flush -> read-back sequence of POST
# /inventory/collect (review round, Finding 1). `cash_collections` has no
# tie-break column that record_cash_collection's own ts (time.time()) can
# guarantee is unique -- back-to-back calls on this platform's timer
# granularity can and do land on the exact same ts (verified by direct
# probe: 1000/1000 identical). Ordering the read-back by `id` instead of
# `ts` (below) fixes "which row is newest" in isolation, but does not by
# itself stop one request's read from picking up a DIFFERENT request's
# row: without serialization, request A's enqueue -> request B's enqueue
# -> request B's read (sees the newest id, which is B's own, correctly)
# -> request A's read (also sees B's row as the newest id, since it now
# exists) is exactly the exec order that shows A the wrong figure. Holding
# this lock for the whole enqueue+flush+read unit means no other request's
# job can be enqueued while one request is still reading, so "the newest
# row when I read it" and "the row my own call caused" are the same row.
#
# Single-process limitation: this is an in-process asyncio.Lock, which
# only serializes coroutines sharing one event loop. main.py runs exactly
# one uvicorn server on one event loop in one process, so that is
# sufficient here -- it would NOT be if this deployment ever grew to
# multiple worker processes (e.g. `uvicorn --workers N`), since each
# worker has its own lock and none of them would see the others' requests.
_collect_cash_lock = asyncio.Lock()


def _max_cash_collection_id(db_path: str) -> int:
    """The current highest `id` in `cash_collections`, or 0 if the table
    is empty. Captured *before* enqueueing so the read-back can later
    prove a specific new row was actually written (Finding 2) rather than
    merely that *some* row exists."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(id) FROM cash_collections").fetchone()
        return row[0] if row and row[0] is not None else 0
    finally:
        conn.close()


def _latest_cash_collection_row(db_path: str):
    """The newest `cash_collections` row by `id` (unambiguous, unlike
    `ts` -- see `_collect_cash_lock` above), or None if the table is
    empty. Runs its connect+query synchronously; callers must run this
    via `asyncio.to_thread` (Finding 4) -- a blocking disk read has no
    business running directly in the coroutine body on this project's
    SD-card target."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, ts, expected_cash FROM cash_collections "
            "ORDER BY id DESC LIMIT 1"
        )
        return cursor.fetchone()
    finally:
        conn.close()


def _find_product(sku: str):
    return next((p for p in context.config.products if p.sku == sku), None)


def _get_or_404(sku: str):
    """An unknown SKU is a shell 404 on both GET and POST (brief resolution
    9) — never a bare JSON 404."""
    product = _find_product(sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No such product: {sku}")
    return product


def _row_for(product) -> dict:
    """This row's template data. With no InventoryManager wired, nothing
    is tracked (brief resolution 4) — the page still renders, every
    product simply falls into the untracked group, rather than failing."""
    tracked = (
        context.inventory_manager.is_tracked(product.sku)
        if context.inventory_manager
        else False
    )
    row = {
        "sku": product.sku,
        "name": product.name,
        "slot": product.slot,
        "tracked": tracked,
    }
    if tracked:
        row["count"] = context.inventory_manager.get_count(product.sku)
    return row


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/inventory",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.edit_placement))],
    )
    async def inventory_view(request: Request):
        rows = [_row_for(p) for p in context.config.products]
        # Tracked rows first, untracked at the bottom (brief resolution 8)
        # — decided once here, never left to the template to sort out.
        ordered = [r for r in rows if r["tracked"]] + [
            r for r in rows if not r["tracked"]
        ]
        return templates.TemplateResponse(
            "inventory.html",
            context.template_context(request, level=LEVEL_INVENTORY, rows=ordered),
        )

    @router.post(
        "/inventory/{sku}/adjust",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.edit_placement)),
            Depends(context.require_htmx),
        ],
    )
    async def adjust_inventory(request: Request, sku: str, delta: str = Form(...)):
        product = _get_or_404(sku)

        # delta is declared as `str`, not `int` — FastAPI's own validation
        # would otherwise turn a non-integer form value into a 422 before
        # this handler ever runs, and the brief is explicit that a
        # rejected delta is a 400 (brief resolution 6). Parsed and
        # range-checked by hand instead, so a non-integer ("abc") and an
        # out-of-range integer ("7") both take the same path to the same
        # 400, leaving the stored count untouched.
        try:
            delta_value = int(delta)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="delta must be one of -10, -1, 1, 10"
            ) from exc
        if delta_value not in _ALLOWED_DELTAS:
            raise HTTPException(
                status_code=400, detail="delta must be one of -10, -1, 1, 10"
            )

        if context.inventory_manager:
            # Clamp at zero rather than erroring — a loader tapping -10 on
            # a count of 3 should land on 0, the physically meaningful
            # result (brief resolution 5). Deliberately different from the
            # placement form, which rejects a typed negative count outright.
            current = context.inventory_manager.get_count(sku)
            context.inventory_manager.set_count(sku, max(0, current + delta_value))

        # One row, not the whole list (brief resolution 7) — a loader's
        # repeated taps must not scroll-jump the page on a tablet.
        return templates.TemplateResponse(
            "partials/inventory_row.html",
            context.template_context(request, row=_row_for(product)),
        )

    @router.get(
        "/inventory/collect/confirm",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.collect_cash))],
    )
    async def collect_confirm(
        request: Request, confirming: str | None = Query(default=None)
    ):
        """confirm_button.html's confirm_url contract: absent or anything but
        the literal string "false" renders the confirming (Confirm/Cancel)
        state; "false" renders the plain first-tap button -- this is what its
        Cancel button sends via hx-vals."""
        return templates.TemplateResponse(
            "partials/confirm_button.html",
            context.template_context(
                request,
                label="Collect Cash",
                confirm_label="Confirm Collection",
                post_url="/inventory/collect",
                target="#collect-cash-confirm",
                confirm_url="/inventory/collect/confirm",
                confirming=(confirming != "false"),
            ),
        )

    @router.post(
        "/inventory/collect",
        response_class=HTMLResponse,
        dependencies=[
            Depends(web_auth.require(Permission.collect_cash)),
            Depends(context.require_htmx),
        ],
    )
    async def collect_cash(request: Request):
        """Record a cash collection and return the recorded time and expected amount."""
        if not context.event_recorder:
            raise HTTPException(status_code=500, detail="Event recorder not configured")

        # Depends(web_auth.require(Permission.collect_cash)) above has
        # already guaranteed an authenticated principal; this re-derivation
        # is defensive only (current_principal returning None here would
        # mean the dependency itself is broken), kept as a belt-and-braces
        # guard rather than trusted to never change.
        principal = web_auth.current_principal(request)
        if not principal:
            raise HTTPException(status_code=401, detail="Not authenticated")

        user_id = principal.user.id
        user_name = principal.user.name
        db_path = context.event_recorder._db_path

        async with _collect_cash_lock:
            # Captured before enqueueing (Finding 2): proves, after the
            # read-back below, whether a genuinely NEW row was written by
            # THIS call, rather than trusting that any row present at all
            # means this call's write landed.
            baseline_id = await asyncio.to_thread(_max_cash_collection_id, db_path)

            # Record the cash collection, which enqueues it on the writer
            # thread.
            context.event_recorder.record_cash_collection(user_id, user_name)

            # Flush the queue to ensure the row is written. If the writer
            # thread has died, this returns immediately without waiting --
            # see EventRecorder.flush's docstring -- which is exactly why
            # the id captured above matters: nothing below may assume the
            # enqueued job was actually processed.
            await asyncio.to_thread(context.event_recorder.flush)

            # Read the row back to get the expected_cash value that was
            # computed in the writer thread at insert time (this ensures
            # consistency with the recorded value, not a figure recomputed
            # here in the route). Ordered by `id`, not `ts` -- see
            # `_collect_cash_lock`'s comment for why `ts` alone cannot
            # disambiguate the newest row. Runs in a thread (Finding 4):
            # the connect+query is a blocking disk read like `flush()`
            # itself, and must not run directly in the coroutine body on
            # this project's SD-card target.
            row = await asyncio.to_thread(_latest_cash_collection_row, db_path)

        if row is None or row[0] <= baseline_id:
            # Either the table is empty (nothing was ever written) or the
            # newest row is no newer than what existed before this call
            # enqueued its job -- the writer thread did not process it (a
            # dead writer thread; see flush()'s docstring). Showing the
            # collector *any* older row here would be showing them someone
            # else's already-recorded collection as if it were their own.
            raise HTTPException(
                status_code=500,
                detail="Cash collection was not recorded (writer thread not running)",
            )

        _row_id, ts, expected_cash = row

        # Format the recorded time as a human-readable string.
        recorded_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        return templates.TemplateResponse(
            "partials/cash_collection_result.html",
            context.template_context(
                request,
                recorded_time=recorded_time,
                expected_cash=expected_cash,
            ),
        )

    return router
