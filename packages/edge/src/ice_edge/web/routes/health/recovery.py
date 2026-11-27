"""The PAY-104 record-sale and discard recovery flow under /health/faults/PAY-104."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from ice_contracts.board.vending_machine import (
    FaultCode,
)
from ice_edge.access.access import Permission
from ice_edge.sale.session_evidence import DiscardOutcome, RecordOutcome
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_edge.web.routes.health.faults import _render_fault_list_oob, dom_safe_key

router = APIRouter()


@router.get(
    "/health/faults/PAY-104/record-sale/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.clear_faults))],
)
async def pay104_record_sale_confirm(
    request: Request, confirming: bool = Depends(context.confirming_flag)
):
    """Same confirm_url contract as fault_clear_confirm above."""
    dom_key = dom_safe_key(FaultCode.PAY_104.value)
    return context.render_confirm(
        request,
        "partials/confirm_button.html",
        label="Record sale",
        confirm_label="Confirm",
        post_url="/health/faults/PAY-104/record-sale",
        target=f"#record-sale-{dom_key}",
        confirm_url="/health/faults/PAY-104/record-sale/confirm",
        confirming=confirming,
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
    """Record as sale: write the pending sale, then clear PAY-104.

    The check-write-clear sequence, its ordering and idempotency live in
    `SessionEvidence.record_pending_sale`; this maps each outcome to the
    response. The recorder is `context.event_recorder`, passed in so route
    tests can swap it.
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
    outcome = await machine.evidence.record_pending_sale(context.event_recorder)
    match outcome:
        case RecordOutcome.NO_RECORDER:
            raise HTTPException(
                status_code=500,
                detail="No event recorder attached; cannot record sale",
            )
        case RecordOutcome.ALREADY_RESERVED | RecordOutcome.MARKER_UNWRITABLE:
            raise HTTPException(status_code=500, detail=marker_unwritable_detail)
        case RecordOutcome.RECORD_FAILED:
            raise HTTPException(
                status_code=500,
                detail="Could not record the sale; PAY-104 left active for retry",
            )
        case RecordOutcome.CASH_SALE_NOT_QUEUED:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Sale recorded, but the board could not be told its cash "
                    "left escrow; PAY-104 left active -- retry (the sale will "
                    "not be recorded twice)"
                ),
            )
        case RecordOutcome.CLEAR_FAILED_MARKER_WRITTEN:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Sale recorded; PAY-104 needs operator attention "
                    "(the snapshot could not be cleared automatically) "
                    "-- retrying will not record the sale again"
                ),
            )
    # NOTHING_PENDING (already handled: a money-safe no-op) and RECORDED.
    return _render_fault_list_oob(request)


@router.get(
    "/health/faults/PAY-104/discard/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.clear_faults))],
)
async def pay104_discard_confirm(
    request: Request, confirming: bool = Depends(context.confirming_flag)
):
    dom_key = dom_safe_key(FaultCode.PAY_104.value)
    return context.render_confirm(
        request,
        "partials/confirm_button.html",
        label="Discard",
        confirm_label="Confirm",
        post_url="/health/faults/PAY-104/discard",
        target=f"#discard-{dom_key}",
        confirm_url="/health/faults/PAY-104/discard/confirm",
        confirming=confirming,
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

    Idempotent: if PAY-104 is no longer active this is a no-op, never a 404.
    Shares its lock with Record as sale inside `SessionEvidence`.
    """
    vmc = context.vmc_instance
    machine = context.machine_instance
    if vmc is None:
        raise HTTPException(status_code=404, detail="No VMC attached")
    if await machine.evidence.discard() is DiscardOutcome.COULD_NOT_CLEAR:
        raise HTTPException(status_code=500, detail="Could not clear PAY-104")
    return _render_fault_list_oob(request)
