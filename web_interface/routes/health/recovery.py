"""The PAY-104 record-sale and discard recovery flow under /health/faults/PAY-104."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from ice_contracts.board.vending_machine import (
    FaultCode,
)
from ice_edge.access.access import Permission
from web_interface import auth as web_auth
from web_interface import context
from ice_ui.templating import templates
from web_interface.routes.health.faults import _render_fault_list_oob, dom_safe_key

router = APIRouter()


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
    dom_key = dom_safe_key(FaultCode.PAY_104.value)
    return templates.TemplateResponse(
        request,
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
    `machine.reserve_pending_sale`/`pending_sale_already_recorded` still
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
    machine = context.machine_instance
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
        pending = machine.pending_sale_for_recovery()
        if pending is None:
            # Already recorded/discarded/cleared by an earlier request
            # -- nothing to do. Money-safe no-op, not an error.
            return _render_fault_list_oob(request)
        if machine.pending_sale_already_recorded(pending):
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
            vmc.raise_fault(
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
            machine.reserve_pending_sale(pending)
            if machine.mark_pending_sale_recorded():
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
            vmc.raise_fault(
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
    dom_key = dom_safe_key(FaultCode.PAY_104.value)
    return templates.TemplateResponse(
        request,
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
